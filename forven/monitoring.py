"""Operational monitors for strategy decay and execution quality."""

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev

from forven.db import get_db, kv_get, kv_set, log_activity
from forven.policy import load_pipeline_config

# P4-8: Shadow-mainnet acceptable gap thresholds
_SHADOW_MAX_SLIPPAGE_BPS = 15.0  # Max acceptable slippage delta
_SHADOW_MAX_RATE_LIMIT_FAILURES = 3  # Max tolerated rate-limit failures in shadow window

log = logging.getLogger("forven.monitoring")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execution_pattern_for_stage(stage: object) -> str | None:
    normalized = str(stage or "").strip().lower()
    if normalized.startswith("paper"):
        return "paper%"
    if normalized.startswith("live") or normalized.startswith("deploy"):
        return "live%"
    return None


def _to_float(value, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_json_obj(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _extract_baseline_sharpe(metrics_raw) -> float | None:
    metrics = _parse_json_obj(metrics_raw)
    baseline = metrics.get("sharpe")
    if baseline is None:
        baseline = metrics.get("sharpe_ratio")
    return _to_float(baseline)


def _annualized_sharpe(pnls: list[float], window_hours: int) -> float:
    if len(pnls) < 2:
        return 0.0
    std = pstdev(pnls)
    if std <= 1e-12:
        return 0.0
    trades_per_year = len(pnls) / max(window_hours / 8760, 1e-9)
    return float((mean(pnls) / std) * math.sqrt(trades_per_year))


def _calc_slippage_bps(signal_price: float, fill_price: float, side: str) -> float:
    if signal_price <= 0:
        return 0.0
    if side == "buy":
        return (fill_price - signal_price) / signal_price * 10_000
    return (signal_price - fill_price) / signal_price * 10_000


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    idx = (len(ordered) - 1) * p
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[lo]
    frac = idx - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def run_decay_tracker(
    demote_status: str = "archived",
    window_hours: int | None = None,
    degradation_threshold: float | None = None,
    min_trades: int | None = None,
) -> dict:
    """Detect live performance decay and demote degraded strategies immediately."""
    config = load_pipeline_config()
    decay_cfg = config.get("decay", {})
    
    if window_hours is None:
        window_hours = int(decay_cfg.get("window_hours", 72))
    if degradation_threshold is None:
        degradation_threshold = float(decay_cfg.get("degradation_threshold", 0.30))
    if min_trades is None:
        min_trades = int(decay_cfg.get("min_trades", 5))

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    now = _now_iso()

    reviewed = 0
    skipped = []
    demoted = []
    demotion_candidates = []

    with get_db() as conn:
        strategies = conn.execute(
            "SELECT id, name, stage, status, metrics, notes, owner FROM strategies "
            "WHERE COALESCE(stage, status) IN ('paper', 'paper_trading', 'live_graduated', 'deployed')"
        ).fetchall()

        for row in strategies:
            strategy = dict(row)
            reviewed += 1
            strategy_id = strategy["id"]
            current_stage = str(strategy.get("stage") or strategy.get("status") or "quick_screen").strip()
            execution_pattern = _execution_pattern_for_stage(current_stage)
            if execution_pattern is None:
                skipped.append({"strategy_id": strategy_id, "reason": "unsupported_decay_stage"})
                continue
            baseline_sharpe = _extract_baseline_sharpe(strategy.get("metrics"))
            if baseline_sharpe is None or baseline_sharpe <= 0:
                skipped.append({"strategy_id": strategy_id, "reason": "missing_or_nonpositive_baseline_sharpe"})
                continue

            trade_rows = conn.execute(
                """SELECT pnl_pct FROM trades
                   WHERE COALESCE(strategy_id, strategy) = ?
                     AND status = 'CLOSED'
                     AND pnl_pct IS NOT NULL
                     AND LOWER(COALESCE(execution_type, '')) LIKE ?
                     AND datetime(closed_at) >= datetime(?)""",
                (strategy_id, execution_pattern, cutoff),
            ).fetchall()
            pnls = [_to_float(r["pnl_pct"], 0.0) for r in trade_rows]
            pnls = [p for p in pnls if p is not None]

            if len(pnls) < min_trades:
                skipped.append(
                    {
                        "strategy_id": strategy_id,
                        "reason": "insufficient_live_trades",
                        "trade_count": len(pnls),
                    }
                )
                continue

            live_sharpe = _annualized_sharpe(pnls, window_hours)
            degradation = 1 - (live_sharpe / baseline_sharpe)

            if degradation <= degradation_threshold:
                continue

            note = (
                f"[{now}] Auto-demoted by decay tracker: live Sharpe {live_sharpe:.2f} vs "
                f"baseline {baseline_sharpe:.2f} ({degradation:.1%} degradation over "
                f"{len(pnls)} closed trades in {window_hours}h)."
            )
            existing_notes = strategy.get("notes") or ""
            merged_notes = f"{existing_notes}\n{note}".strip()

            demotion_candidates.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_name": strategy.get("name") or strategy_id,
                    "status_before": current_stage,
                    "status_after": demote_status,
                    "owner_before": str(strategy.get("owner") or "").strip() or None,
                    "baseline_sharpe": round(baseline_sharpe, 4),
                    "live_sharpe_72h": round(live_sharpe, 4),
                    "degradation": round(degradation, 4),
                    "trade_count_72h": len(pnls),
                    "note": note,
                    "merged_notes": merged_notes,
                }
            )

    if demotion_candidates:
        from forven.brain import transition_stage

    for candidate in demotion_candidates:
        strategy_id = candidate["strategy_id"]
        try:
            transition = transition_stage(
                strategy_id=strategy_id,
                target_stage=demote_status,
                reason=candidate["note"],
                actor="decay_tracker",
                notes=candidate["merged_notes"],
                evidence={
                    "trigger": "decay_tracker",
                    "baseline_sharpe": candidate["baseline_sharpe"],
                    "live_sharpe_72h": candidate["live_sharpe_72h"],
                    "degradation": candidate["degradation"],
                    "trade_count_72h": candidate["trade_count_72h"],
                    "window_hours": window_hours,
                    "degradation_threshold": degradation_threshold,
                },
            )
            status_after = str(transition.get("to") or demote_status)
            candidate["status_after"] = status_after
            if status_after != demote_status:
                skipped.append(
                    {
                        "strategy_id": strategy_id,
                        "reason": "approval_required",
                        "reason_code": transition.get("reason_code"),
                        "requested_status": demote_status,
                        "current_status": status_after,
                        "approval_id": transition.get("approval_id"),
                    }
                )
                continue

            with get_db() as conn:
                conn.execute(
                    """INSERT INTO strategy_decay_audit
                       (strategy_id, status_before, status_after, baseline_sharpe, live_sharpe_72h,
                        degradation, trade_count_72h, triggered_at, reason)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        strategy_id,
                        candidate["status_before"] or "unknown",
                        status_after,
                        round(float(candidate["baseline_sharpe"]), 6),
                        round(float(candidate["live_sharpe_72h"]), 6),
                        round(float(candidate["degradation"]), 6),
                        int(candidate["trade_count_72h"]),
                        now,
                        f"degradation>{degradation_threshold:.0%}",
                    ),
                )
            demoted.append(candidate)
        except Exception as exc:
            skipped.append(
                {
                    "strategy_id": strategy_id,
                    "reason": "transition_failed",
                    "error": str(exc),
                }
            )

    for event in demoted:
        msg = (
            f"Decay demotion: {event['strategy_id']} {event['status_before']}->{event['status_after']} "
            f"(baseline Sharpe {event['baseline_sharpe']:.2f}, live {event['live_sharpe_72h']:.2f}, "
            f"degradation {event['degradation']:.1%})"
        )
        log.warning(msg)
        log_activity("warning", "decay-tracker", msg, event)

    # Queue immediate re-optimization work for degraded strategies.
    if demoted:
        try:
            from forven.brain import assign_task

            for event in demoted:
                assign_task(
                    agent_id="simulation-agent",
                    task_type="backtest",
                    title=f"Decay Re-Optimization: {event['strategy_id']}",
                    input_data={
                        "strategy_id": event["strategy_id"],
                    },
                    description=(
                        f"STRATEGY DECAY ALERT — {event['strategy_id']} was auto-demoted.\n\n"
                        f"- Baseline backtest Sharpe: {event['baseline_sharpe']:.2f}\n"
                        f"- Live 72h Sharpe: {event['live_sharpe_72h']:.2f}\n"
                        f"- Degradation: {event['degradation']:.1%}\n"
                        f"- Closed trades in window: {event['trade_count_72h']}\n\n"
                        "Tasks:\n"
                        "1. Re-run backtests and walk-forward validation with current data.\n"
                        "2. Review trade_slippage_audit for this strategy and incorporate realistic slippage.\n"
                        "3. Propose parameter updates or a replacement strategy.\n"
                        "4. Store results in backtest_results and summarize recommendations."
                    ),
                )
        except Exception as e:
            # C18: log_activity persists this failure to the audit table so
            # operator can see why demoted strategies aren't getting follow-up
            # tasks (otherwise it's invisible — only a log warning).
            log.warning("Could not queue re-optimization task(s): %s", e)
            try:
                log_activity(
                    "error",
                    "decay-tracker",
                    f"Failed to queue re-optimization tasks for {len(demoted)} demoted strategies: {e}",
                    {
                        "demoted_count": len(demoted),
                        "demoted_ids": [event["strategy_id"] for event in demoted],
                        "error": str(e),
                    },
                )
            except Exception:
                pass

    summary = {
        "ran_at": now,
        "window_hours": window_hours,
        "degradation_threshold": degradation_threshold,
        "min_trades": min_trades,
        "reviewed": reviewed,
        "demoted_count": len(demoted),
        "demoted": demoted,
        "skipped_count": len(skipped),
        "skipped": skipped[:50],
    }
    kv_set("strategy_decay_state", summary)
    return summary


def _emit_kill_switch_notification(
    *,
    strategy_id: str,
    archived: bool,
    degradation: float,
    kill_switch_pct: float,
    live_sharpe: float,
    baseline_sharpe: float,
    blocked_reason: object,
    reason_code: object,
    trigger_payload: dict,
) -> None:
    """Alert the operator that the decay kill-switch fired (B-34).

    ``risk_critical`` routes to the risk Discord channel and is on by default
    (notification_policy). The BLOCKED case — kill-switch fired but the halt
    transition did not land, so the strategy is STILL TRADING — gets a distinct
    message and dedupe key so it can never be deduped against an earlier
    successful-archive alert. Best-effort: a notification failure must never
    break the kill-switch itself.
    """
    try:
        from forven.notifications import emit_notification

        if archived:
            title = f"Decay kill-switch ARCHIVED {strategy_id}"
            summary = (
                f"Live Sharpe {live_sharpe:.2f} vs baseline {baseline_sharpe:.2f} "
                f"({degradation:.1%} degradation > {kill_switch_pct:.0%} threshold). "
                "Strategy halted (archived)."
            )
        else:
            title = f"Decay kill-switch BLOCKED — {strategy_id} still LIVE"
            summary = (
                f"Degradation {degradation:.1%} exceeds the {kill_switch_pct:.0%} threshold "
                f"but the halt transition was blocked "
                f"({blocked_reason or reason_code or 'unknown'}). "
                "The strategy is STILL TRADING — manual intervention required."
            )
        emit_notification(
            "risk_critical",
            severity="critical",
            source="decay_kill_switch",
            title=title,
            summary=summary,
            metadata=dict(trigger_payload),
            dedupe_key=(
                f"decay_kill_switch:{strategy_id}:{'archived' if archived else 'blocked'}"
            ),
        )
    except Exception:
        log.warning(
            "Failed to emit decay kill-switch notification for %s", strategy_id,
            exc_info=True,
        )


def run_decay_kill_switch() -> dict:
    """P1-10: Hourly decay kill-switch — pause execution immediately on threshold breach.

    Compares rolling live performance against graduation baseline.
    Triggers immediate execution pause when ``decay_kill_switch_pct`` breach is detected.
    """
    config = load_pipeline_config()
    live_cfg = config.get("live_graduated", {})
    kill_switch_pct = float(live_cfg.get("decay_kill_switch_pct", 0.30))
    window_hours = int(config.get("decay", {}).get("window_hours", 72))
    min_trades = int(config.get("decay", {}).get("min_trades", 5))
    now = _now_iso()

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    triggered = []
    reviewed = 0

    with get_db() as conn:
        strategies = conn.execute(
            "SELECT id, name, stage, metrics FROM strategies "
            "WHERE COALESCE(stage, status) IN ('live_graduated', 'deployed')"
        ).fetchall()

        for row in strategies:
            strategy = dict(row)
            reviewed += 1
            strategy_id = strategy["id"]

            baseline_sharpe = _extract_baseline_sharpe(strategy.get("metrics"))
            if baseline_sharpe is None or baseline_sharpe <= 0:
                continue

            trade_rows = conn.execute(
                """SELECT pnl_pct FROM trades
                   WHERE COALESCE(strategy_id, strategy) = ?
                     AND status = 'CLOSED' AND pnl_pct IS NOT NULL
                     AND LOWER(COALESCE(execution_type, '')) LIKE 'live%'
                     AND datetime(closed_at) >= datetime(?)""",
                (strategy_id, cutoff),
            ).fetchall()
            pnls = [_to_float(r["pnl_pct"], 0.0) for r in trade_rows]
            pnls = [p for p in pnls if p is not None]

            if len(pnls) < min_trades:
                continue

            live_sharpe = _annualized_sharpe(pnls, window_hours)
            degradation = 1 - (live_sharpe / baseline_sharpe)

            if degradation <= kill_switch_pct:
                continue

            # KILL SWITCH TRIGGERED — pause execution immediately
            trigger_payload = {
                "strategy_id": strategy_id,
                "strategy_name": strategy.get("name") or strategy_id,
                "baseline_sharpe": round(baseline_sharpe, 4),
                "live_sharpe": round(live_sharpe, 4),
                "degradation": round(degradation, 4),
                "kill_switch_pct": kill_switch_pct,
                "trade_count": len(pnls),
                "window_hours": window_hours,
                "triggered_at": now,
            }

            # Halt execution by transitioning to archived. decay_kill_switch is a
            # designated system SAFETY actor (brain._SYSTEM_FORCE_ACTORS), so force=True
            # is honoured and bypasses the dethrone-approval gate — the strategy is
            # actually stopped rather than parked behind an operator approval that never
            # comes in headless operation. A canonical strategy still stays protected
            # (handled below by inspecting the returned transition, not assumed).
            transition: dict | None = None
            try:
                from forven.brain import transition_stage
                transition = transition_stage(
                    strategy_id=strategy_id,
                    target_stage="archived",
                    reason=(
                        f"DECAY KILL-SWITCH: {degradation:.1%} degradation exceeds "
                        f"{kill_switch_pct:.0%} threshold (live Sharpe {live_sharpe:.2f} "
                        f"vs baseline {baseline_sharpe:.2f})"
                    ),
                    actor="decay_kill_switch",
                    force=True,
                )
            except Exception as exc:
                log.error("Kill-switch transition failed for %s: %s", strategy_id, exc)
                trigger_payload["transition_error"] = str(exc)

            # Report HONESTLY: only claim 'archived' if the transition actually landed
            # there. A blocked transition (e.g. canonical protection) returns the
            # current stage and a blocked_reason — record that, don't fake an archive.
            actual_stage = str((transition or {}).get("to") or "live_graduated").lower()
            archived = actual_stage == "archived"
            blocked_reason = (transition or {}).get("blocked_reason")
            reason_code = (transition or {}).get("reason_code")
            approval_id = (transition or {}).get("approval_id")
            trigger_payload["archived"] = archived
            trigger_payload["status_after"] = actual_stage
            if not archived:
                if blocked_reason:
                    trigger_payload["blocked_reason"] = blocked_reason
                if reason_code:
                    trigger_payload["reason_code"] = reason_code
                if approval_id:
                    trigger_payload["approval_id"] = approval_id

            # Persist structured trigger payload (status_after + reason reflect reality)
            try:
                with get_db() as conn2:
                    conn2.execute(
                        """INSERT INTO strategy_decay_audit
                           (strategy_id, status_before, status_after, baseline_sharpe,
                            live_sharpe_72h, degradation, trade_count_72h, triggered_at, reason)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            strategy_id, "live_graduated", actual_stage,
                            round(baseline_sharpe, 6), round(live_sharpe, 6),
                            round(degradation, 6), len(pnls), now,
                            f"kill_switch>{kill_switch_pct:.0%}" if archived
                            else f"kill_switch_not_halted:{reason_code or 'blocked'}",
                        ),
                    )
            except Exception:
                pass

            if archived:
                log.critical(
                    "DECAY KILL-SWITCH TRIGGERED: %s — degradation %.1f%% > threshold %.0f%% (ARCHIVED)",
                    strategy_id, degradation * 100, kill_switch_pct * 100,
                )
                log_activity("critical", "decay-kill-switch", f"Kill switch: {strategy_id}", trigger_payload)
            else:
                log.critical(
                    "DECAY KILL-SWITCH could NOT halt %s — degradation %.1f%% > %.0f%% "
                    "but transition blocked (%s); strategy still active",
                    strategy_id, degradation * 100, kill_switch_pct * 100,
                    blocked_reason or "unknown",
                )
                log_activity(
                    "critical", "decay-kill-switch",
                    f"Kill switch BLOCKED: {strategy_id} still active ({reason_code or 'blocked'})",
                    trigger_payload,
                )
            _emit_kill_switch_notification(
                strategy_id=strategy_id,
                archived=archived,
                degradation=degradation,
                kill_switch_pct=kill_switch_pct,
                live_sharpe=live_sharpe,
                baseline_sharpe=baseline_sharpe,
                blocked_reason=blocked_reason,
                reason_code=reason_code,
                trigger_payload=trigger_payload,
            )
            triggered.append(trigger_payload)

    summary = {
        "ran_at": now,
        "kill_switch_pct": kill_switch_pct,
        "reviewed": reviewed,
        "triggered_count": len(triggered),
        "triggered": triggered,
    }
    kv_set("decay_kill_switch_state", summary)
    return summary


def run_slippage_monitor(
    lookback_hours: int = 168,
    max_trades: int = 2000,
) -> dict:
    """Compute signal vs fill slippage and store audit rows."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    analyzed_at = _now_iso()

    samples = []
    changed_samples = []

    with get_db() as conn:
        candidates = conn.execute(
            """SELECT id, COALESCE(strategy_id, strategy) as strategy_id, asset, direction,
                      signal_entry_price, fill_entry_price,
                      signal_exit_price, fill_exit_price
                 FROM trades
                WHERE datetime(COALESCE(closed_at, opened_at)) >= datetime(?)
                  AND (
                    (signal_entry_price IS NOT NULL AND fill_entry_price IS NOT NULL) OR
                    (signal_exit_price IS NOT NULL AND fill_exit_price IS NOT NULL)
                  )
                ORDER BY opened_at DESC
                LIMIT ?""",
            (cutoff, max_trades),
        ).fetchall()

        existing_rows = conn.execute(
            "SELECT trade_id, leg, COALESCE(strategy_id, strategy) as strategy_id, signal_price, fill_price, slippage_bps FROM trade_slippage_audit"
        ).fetchall()
        existing_map = {
            (r["trade_id"], r["leg"]): (
                _to_float(r["signal_price"], 0.0) or 0.0,
                _to_float(r["fill_price"], 0.0) or 0.0,
                _to_float(r["slippage_bps"], 0.0) or 0.0,
            )
            for r in existing_rows
        }

        for row in candidates:
            trade = dict(row)
            trade_id = trade["id"]
            direction = (trade.get("direction") or "long").lower()
            strategy = trade.get("strategy_id") or trade.get("strategy") or "unknown"
            asset = (trade.get("asset") or "").upper()

            legs = [
                ("entry", _to_float(trade.get("signal_entry_price")), _to_float(trade.get("fill_entry_price"))),
                ("exit", _to_float(trade.get("signal_exit_price")), _to_float(trade.get("fill_exit_price"))),
            ]

            for leg, signal_price, fill_price in legs:
                if signal_price is None or fill_price is None or signal_price <= 0:
                    continue

                if leg == "entry":
                    side = "buy" if direction == "long" else "sell"
                else:
                    side = "sell" if direction == "long" else "buy"

                slippage_bps = _calc_slippage_bps(signal_price, fill_price, side)
                abs_slippage_bps = abs((fill_price - signal_price) / signal_price * 10_000)
                sample = {
                    "trade_id": trade_id,
                    "strategy": strategy,
                    "asset": asset,
                    "direction": direction,
                    "leg": leg,
                    "signal_price": float(signal_price),
                    "fill_price": float(fill_price),
                    "slippage_bps": round(float(slippage_bps), 6),
                    "abs_slippage_bps": round(float(abs_slippage_bps), 6),
                    "analyzed_at": analyzed_at,
                }
                samples.append(sample)

                prev = existing_map.get((trade_id, leg))
                if prev is None or (
                    abs(prev[0] - sample["signal_price"]) > 1e-9
                    or abs(prev[1] - sample["fill_price"]) > 1e-9
                    or abs(prev[2] - sample["slippage_bps"]) > 1e-6
                ):
                    changed_samples.append(sample)

                conn.execute(
                    """INSERT INTO trade_slippage_audit
                       (trade_id, strategy, strategy_id, asset, direction, leg, signal_price, fill_price,
                        slippage_bps, abs_slippage_bps, analyzed_at, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'slippage_monitor')
                       ON CONFLICT(trade_id, leg) DO UPDATE SET
                         strategy = excluded.strategy,
                         strategy_id = excluded.strategy_id,
                         asset = excluded.asset,
                         direction = excluded.direction,
                         signal_price = excluded.signal_price,
                         fill_price = excluded.fill_price,
                         slippage_bps = excluded.slippage_bps,
                         abs_slippage_bps = excluded.abs_slippage_bps,
                         analyzed_at = excluded.analyzed_at,
                         source = excluded.source""",
                    (
                        sample["trade_id"],
                        sample["strategy"],
                        sample["strategy"],
                        sample["asset"],
                        sample["direction"],
                        sample["leg"],
                        sample["signal_price"],
                        sample["fill_price"],
                        sample["slippage_bps"],
                        sample["abs_slippage_bps"],
                        sample["analyzed_at"],
                    ),
                )

                slippage_col = "entry_slippage_bps" if leg == "entry" else "exit_slippage_bps"
                conn.execute(
                    f"UPDATE trades SET {slippage_col} = ? WHERE id = ?",
                    (sample["slippage_bps"], trade_id),
                )

        penalty_rows = conn.execute(
            """SELECT COALESCE(strategy_id, strategy) as strategy, slippage_bps
                 FROM trade_slippage_audit
                WHERE datetime(analyzed_at) >= datetime(?)""",
            (cutoff,),
        ).fetchall()

    penalties: dict[str, dict] = {}
    grouped: dict[str, list[float]] = {}
    for row in penalty_rows:
        strategy = row["strategy"] or "unknown"
        bps = _to_float(row["slippage_bps"], 0.0) or 0.0
        grouped.setdefault(strategy, []).append(max(0.0, bps))

    for strategy, adverse in grouped.items():
        penalties[strategy] = {
            "samples": len(adverse),
            "median_adverse_bps": round(_percentile(adverse, 0.50), 4),
            "p75_adverse_bps": round(_percentile(adverse, 0.75), 4),
            "p90_adverse_bps": round(_percentile(adverse, 0.90), 4),
            # Conservative default penalty for backtests.
            "recommended_penalty_bps": round(_percentile(adverse, 0.75), 4),
        }

    summary = {
        "ran_at": analyzed_at,
        "lookback_hours": lookback_hours,
        "candidate_samples": len(samples),
        "changed_samples": len(changed_samples),
        "strategies_with_penalties": len(penalties),
        "penalties": penalties,
    }
    kv_set("slippage_penalty_bps", summary)
    log_activity(
        "info",
        "slippage-monitor",
        (
            f"Slippage monitor analyzed {len(samples)} samples, "
            f"{len(changed_samples)} changed, {len(penalties)} strategies"
        ),
        {"changed_samples": len(changed_samples), "lookback_hours": lookback_hours},
    )
    return summary


# ── Execution-quality watchdog: realized fill skew vs the modeled cost budget ──

def run_execution_quality_watchdog(lookback_days: int = 30, min_trades: int = 5) -> dict:
    """Nightly expected-vs-actual execution check.

    Every kernel paper/live fill records the backtest-EXPECTED price
    (signal_entry/exit_price, the kernel's next-bar-open) next to the actual
    fill (fill_entry/exit_price); entry/exit_slippage_bps is the signed
    adverse gap per leg and entry/exit_lag_bps its decision-lag component
    (the remainder is venue slippage — paper fills at the mark, so its gap is
    pure lag). Fees are already modeled in the trade's net PnL, so the skew
    measured here is cost the backtest did NOT model.

    This job aggregates that skew per strategy (paper and live buckets
    separately) and flags any whose MEAN realized round-trip skew exceeds the
    strategy's modeled round-trip cost budget 2*(fee_bps + slippage_bps):
    execution then eats at least twice the modeled costs, so paper/live
    results no longer predict the backtest.
    """
    from forven.scanner import _resolve_trade_assumptions

    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    ran_at = _now_iso()

    groups: dict[tuple[str, str], list[dict]] = {}
    params_by_sid: dict[str, dict] = {}
    with get_db() as conn:
        rows = conn.execute(
            """SELECT COALESCE(strategy_id, strategy) AS sid, execution_type,
                      entry_slippage_bps, exit_slippage_bps, entry_lag_bps, exit_lag_bps
                 FROM trades
                WHERE status = 'CLOSED'
                  AND datetime(COALESCE(closed_at, opened_at)) >= datetime(?)
                  AND execution_type IN ('paper', 'paper_challenger', 'live')
                  AND (entry_slippage_bps IS NOT NULL OR exit_slippage_bps IS NOT NULL)""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            sid = str(row["sid"] or "unknown")
            bucket = "live" if str(row["execution_type"] or "").strip().lower() == "live" else "paper"
            groups.setdefault((sid, bucket), []).append(dict(row))
        for sid, _bucket in list(groups):
            if sid in params_by_sid:
                continue
            p_row = conn.execute("SELECT params FROM strategies WHERE id = ?", (sid,)).fetchone()
            params_by_sid[sid] = _parse_json_obj(p_row["params"]) if p_row else {}

    checked: list[dict] = []
    flagged: list[dict] = []
    for (sid, bucket), trades in sorted(groups.items()):
        n = len(trades)
        round_trip = [
            (_to_float(t.get("entry_slippage_bps"), 0.0) or 0.0)
            + (_to_float(t.get("exit_slippage_bps"), 0.0) or 0.0)
            for t in trades
        ]
        lag = [
            (_to_float(t.get("entry_lag_bps"), 0.0) or 0.0)
            + (_to_float(t.get("exit_lag_bps"), 0.0) or 0.0)
            for t in trades
        ]
        mean_skew = mean(round_trip)
        mean_lag = mean(lag)
        _, fee_bps, slip_bps = _resolve_trade_assumptions(params_by_sid.get(sid) or {})
        budget_bps = 2.0 * (fee_bps + slip_bps)
        stat = {
            "strategy_id": sid,
            "bucket": bucket,
            "trades": n,
            "mean_round_trip_skew_bps": round(mean_skew, 4),
            "mean_lag_bps": round(mean_lag, 4),
            "mean_venue_slippage_bps": round(mean_skew - mean_lag, 4),
            "budget_round_trip_bps": round(budget_bps, 4),
            "over_budget": bool(n >= min_trades and mean_skew > budget_bps),
        }
        checked.append(stat)
        if not stat["over_budget"]:
            continue
        flagged.append(stat)
        try:
            from forven.notifications import emit_notification

            emit_notification(
                "execution_quality",
                severity="warning",
                source="watchdog",
                title=f"Execution skew over cost budget: {sid} ({bucket})",
                summary=(
                    f"Mean round-trip fill skew {mean_skew:.1f} bps exceeds the modeled cost budget "
                    f"{budget_bps:.1f} bps over {n} {bucket} trades in the last {lookback_days}d "
                    f"(lag {mean_lag:.1f} bps + venue slippage {mean_skew - mean_lag:.1f} bps)."
                ),
                body=(
                    f"{sid} ({bucket}): realized fills are drifting from backtest assumptions by more "
                    f"than the entire modeled round-trip cost budget (2 x (fee {fee_bps:.1f} + slippage "
                    f"{slip_bps:.1f}) = {budget_bps:.1f} bps). This skew is on TOP of modeled costs, so "
                    f"paper/live results for this strategy no longer predict its backtest. Decomposition: "
                    f"decision lag {mean_lag:.1f} bps, venue slippage {mean_skew - mean_lag:.1f} bps."
                ),
                metadata=stat,
                dedupe_key=f"exec_quality:{sid}:{bucket}",
            )
        except Exception as exc:
            log.warning("execution-quality notification failed for %s: %s", sid, exc)

    summary = {
        "ran_at": ran_at,
        "lookback_days": lookback_days,
        "min_trades": min_trades,
        "groups_checked": len(checked),
        "flagged_count": len(flagged),
        "flagged": flagged,
        "groups": checked,
    }
    kv_set("execution_quality_watchdog_state", summary)
    log_activity(
        "info",
        "exec-quality-watchdog",
        (
            f"Execution-quality watchdog checked {len(checked)} strategy buckets, "
            f"{len(flagged)} over cost budget"
        ),
        {"flagged": [f["strategy_id"] for f in flagged], "lookback_days": lookback_days},
    )
    return summary


# ── P4-5: Live-vs-paper drift computation ───────────────────────────────────

_DRIFT_SHARPE_THRESHOLD = 0.50  # Flag if Sharpe degrades > 50%
_DRIFT_DD_INFLATION_THRESHOLD = 2.0  # Flag if DD inflates > 2x


def compute_paper_live_drift(lookback_days: int = 7) -> dict:
    """P4-5: Compute 7-day live-vs-paper delta for all graduated strategies.

    Compares current live performance against graduation baseline snapshot.
    Auto-flags strategies exceeding drift thresholds.
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=lookback_days)).isoformat()
    drift_results = []
    flagged = []

    with get_db() as conn:
        strategies = conn.execute(
            "SELECT id, name, stage FROM strategies "
            "WHERE COALESCE(stage, status) IN ('live_graduated', 'deployed')"
        ).fetchall()

        for row in strategies:
            strategy_id = row["id"]

            # Get graduation baseline
            baseline = kv_get(f"graduation_baseline:{strategy_id}")
            if not isinstance(baseline, dict):
                continue

            baseline_sharpe = float(baseline.get("backtest_sharpe", 0.0) or 0.0)
            baseline_dd = float(baseline.get("backtest_max_dd", 0.0) or 0.0)

            # Get recent live trades
            trade_rows = conn.execute(
                """SELECT pnl_pct FROM trades
                   WHERE COALESCE(strategy_id, strategy) = ?
                     AND status = 'CLOSED' AND pnl_pct IS NOT NULL
                     AND LOWER(COALESCE(execution_type, '')) LIKE 'live%'
                     AND datetime(closed_at) >= datetime(?)""",
                (strategy_id, cutoff),
            ).fetchall()
            pnls = [_to_float(r["pnl_pct"], 0.0) for r in trade_rows]
            pnls = [p for p in pnls if p is not None]

            if len(pnls) < 3:
                continue

            live_sharpe = _annualized_sharpe(pnls, lookback_days * 24)

            # Compute drawdown from PnLs
            cumulative = 0.0
            peak = 0.0
            max_dd = 0.0
            for p in pnls:
                cumulative += p
                if cumulative > peak:
                    peak = cumulative
                dd = peak - cumulative
                if dd > max_dd:
                    max_dd = dd

            sharpe_degradation = (1 - live_sharpe / baseline_sharpe) if baseline_sharpe > 0 else 0.0
            dd_inflation = max_dd / baseline_dd if baseline_dd > 0 else 0.0

            entry = {
                "strategy_id": strategy_id,
                "strategy_name": row["name"],
                "baseline_sharpe": round(baseline_sharpe, 3),
                "live_sharpe": round(live_sharpe, 3),
                "sharpe_degradation": round(sharpe_degradation, 3),
                "baseline_dd": round(baseline_dd, 4),
                "live_dd": round(max_dd, 4),
                "dd_inflation": round(dd_inflation, 2),
                "trade_count": len(pnls),
                "flagged": False,
                "flag_reasons": [],
            }

            if sharpe_degradation > _DRIFT_SHARPE_THRESHOLD:
                entry["flagged"] = True
                entry["flag_reasons"].append(f"sharpe_degradation={sharpe_degradation:.1%}")
            if dd_inflation > _DRIFT_DD_INFLATION_THRESHOLD:
                entry["flagged"] = True
                entry["flag_reasons"].append(f"dd_inflation={dd_inflation:.1f}x")

            drift_results.append(entry)
            if entry["flagged"]:
                flagged.append(entry)
                log.warning(
                    "P4-5 DRIFT ALERT: %s — %s",
                    strategy_id, ", ".join(entry["flag_reasons"]),
                )

    if flagged:
        log_activity(
            "warning", "drift-monitor",
            f"Drift alert: {len(flagged)} strategies flagged",
            {"flagged": flagged},
        )

    drift_summary = {
        "computed_at": now.isoformat(),
        "lookback_days": lookback_days,
        "total_reviewed": len(drift_results),
        "flagged_count": len(flagged),
        "results": drift_results,
    }
    kv_set("paper_live_drift", drift_summary)
    return drift_summary
