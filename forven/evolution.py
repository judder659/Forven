"""Evolution cycle — autonomous strategy lifecycle management.

5-step pipeline: Ideate → Test → Paper → Live → Autopsy

Each step assigns work through the Brain (hub-and-spoke), never directly
to agents. The evolution engine runs on a schedule and manages strategy
state transitions in the database.

Promotion criteria:
- backtesting → paper: fitness >= 60, WFA degradation < 30%, PF > 1.5, DD < 15%
- paper → deployed: 7+ days in paper, fitness >= 70, PF > 1.5
- deployed → retired: fitness < 40, or max DD > 15%
"""

import asyncio
import concurrent.futures
import json
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from forven.brain import assign_task, transition_stage
from forven.db import (
    append_strategy_event,
    build_strategy_container_name,
    get_db,
    get_strategies,
    kv_get,
    kv_set,
    log_activity,
)
from forven.util import normalize_stage
from forven.policy import evaluate_promotion, score_strategy, compute_live_metrics, check_promotion_readiness

log = logging.getLogger("forven.evolution")
_TESTING_STEP_LOCK = threading.Lock()
_TESTING_STEP_RUNNING_SINCE: float | None = None

# Pipeline Thresholds are dynamically loaded from forven.policy.load_pipeline_config()


def _coerce_int(value, default: int, lower: int, upper: int) -> int:
    try:
        parsed = int(float(str(value).strip()))
    except Exception:
        parsed = default
    return max(lower, min(upper, parsed))


def _coerce_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _pipeline_assignments_per_cycle() -> int:
    raw = kv_get("forven:settings", {})
    settings = raw if isinstance(raw, dict) else {}
    return _coerce_int(settings.get("pipeline_assignments_per_cycle"), 10, 1, 100)


def _pipeline_drain_enabled() -> bool:
    raw = kv_get("forven:settings", {})
    settings = raw if isinstance(raw, dict) else {}
    return _coerce_bool(settings.get("pipeline_drain_mode"), True)


def _pipeline_drain_max_seconds() -> int:
    raw = kv_get("forven:settings", {})
    settings = raw if isinstance(raw, dict) else {}
    return _coerce_int(settings.get("pipeline_drain_max_seconds"), 600, 30, 3600)


def _pipeline_gate_failure_archive_attempts() -> int:
    raw = kv_get("forven:settings", {})
    settings = raw if isinstance(raw, dict) else {}
    return _coerce_int(settings.get("pipeline_gate_failure_archive_attempts"), 3, 1, 10)


def _normalize_gate_failure_text(reason: object) -> str:
    text = re.sub(r"\s+", " ", str(reason or "").strip())
    if text.lower().startswith("gate failure:"):
        return text.split(":", 1)[1].strip()
    return text


def _is_terminal_quick_screen_gate_failure(reason: object) -> bool:
    """Return true for quick-screen gate failures that cannot improve by waiting."""
    text = _normalize_gate_failure_text(reason).lower()
    if not text:
        return False

    # Data-quality holds are quarantines for suspected engine/data bugs, not
    # strategy failures — never terminal, no matter what other gate text the
    # combined reason string carries.
    if "dataqualityhold" in text:
        return False

    terminal_markers = (
        "duplicate with active strategy",
        "quick screen reject",
        "overfit reject",
        "s00552 reject",
        "s00152 reject",
        "p25-4 reject",
    )
    if any(marker in text for marker in terminal_markers):
        return True

    # Brain's quick_screen->gauntlet guardrails use explicit "(reject)" hard-gate text.
    if "(reject)" in text:
        return True

    if "insufficient trade count" in text and "sharpe undefined" in text:
        return True

    return False


def _archive_terminal_quick_screen_gate_failure(
    strategy_id: str,
    gate_reason: object,
    *,
    actor: str = "evolution_terminal_archive",
) -> bool:
    # M-13 (2026-06-09 audit): actor='system' was silently downgraded by
    # brain.transition_stage (not in _USER_ACTORS/_SYSTEM_FORCE_ACTORS), which
    # re-enabled ghost protection and blocked the intended terminal archive 323x
    # in 7 days. 'evolution_terminal_archive' is a dedicated _SYSTEM_FORCE_ACTORS
    # member: the archive of a hard "(reject)" verdict actually happens, the
    # canonical guard still blocks, and skill-outcome closure still records
    # (deliberately NOT a _USER_ACTOR).
    reason = _normalize_gate_failure_text(gate_reason)
    if not _is_terminal_quick_screen_gate_failure(reason):
        return False
    transition = transition_stage(
        strategy_id,
        "archived",
        reason=f"Terminal quick-screen gate failure: {reason}",
        actor=actor,
        force=True,
    )
    actual_target = normalize_stage(transition.get("to")) or str(transition.get("to") or "").strip().lower()
    return actual_target == "archived"


def _resolve_pipeline_execution_plan(candidate_count: int) -> dict[str, int | bool]:
    """Resolve testing-cycle throughput based on current settings and queue pressure.

    Adaptive mode targets backlog clearance within `pipeline_target_clear_hours`
    while respecting current claim limits and in-flight backtest load.
    """
    raw = kv_get("forven:settings", {})
    settings = raw if isinstance(raw, dict) else {}

    base_assignments = _coerce_int(settings.get("pipeline_assignments_per_cycle"), 10, 1, 100)
    base_drain = _pipeline_drain_enabled()
    base_budget = _coerce_int(settings.get("pipeline_drain_max_seconds"), 600, 30, 3600)

    adaptive_enabled = _coerce_bool(settings.get("adaptive_pipeline_throughput_enabled"), False)
    target_clear_hours = _coerce_int(settings.get("pipeline_target_clear_hours"), 6, 1, 168)
    testing_interval_minutes = _coerce_int(settings.get("testing_interval_minutes"), 15, 1, 1440)
    agent_claim_limit = _coerce_int(settings.get("agent_task_claim_limit"), 12, 1, 100)
    brain_claim_limit = _coerce_int(settings.get("brain_task_claim_limit"), 12, 1, 100)

    backlog = max(int(candidate_count), 0)

    # Uncapped drain: when drain mode is enabled, process ALL candidates
    # with a time budget proportional to the backlog size.  With parallel
    # matrix validation (~45s per strategy), the budget scales linearly.
    if base_drain and backlog > 0:
        # ~45s per strategy with parallel backtests, plus buffer
        estimated_time = max(base_budget, backlog * 45)
        # Hard cap at 1 hour to prevent scheduler starvation
        drain_budget = min(estimated_time, 3600)
        return {
            "adaptive": True,
            "target_clear_hours": target_clear_hours,
            "max_assignments": backlog,  # Process ALL candidates
            "drain": True,
            "drain_max_seconds": int(drain_budget),
            "throttled": False,
        }

    # Non-drain / no backlog fallback
    return {
        "adaptive": adaptive_enabled,
        "target_clear_hours": target_clear_hours,
        "max_assignments": base_assignments,
        "drain": base_drain,
        "drain_max_seconds": base_budget,
    }


def _strategy_stage(row: dict) -> str:
    return normalize_stage(row.get("stage") or row.get("status"))


# Reference/template containers (e.g. the 'prebuilt' rows an ad-hoc backtest
# against a registry type creates, owner='system') are NOT pipeline candidates.
# normalize_stage() silently maps an unknown raw stage like 'prebuilt' to
# 'quick_screen', so without this guard the testing cycle pulls them in and
# assigns WFA tasks the simulation-agent can never claim (owner='system' fails the
# container lock), dead-lettering 100s of tasks. Match the RAW stage/owner.
_NON_PIPELINE_RAW_STAGES = {"prebuilt", "template", "reference", "catalog"}


def _is_pipeline_candidate_strategy(row: dict) -> bool:
    raw_stage = str(row.get("stage") or row.get("status") or "").strip().lower()
    if raw_stage in _NON_PIPELINE_RAW_STAGES:
        return False
    # A pipeline strategy in quick_screen/gauntlet is owned by simulation-agent;
    # owner='system' marks a non-pipeline reference container.
    if str(row.get("owner") or "").strip().lower() == "system":
        return False
    return True


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


# ── P3-3: Archetype fingerprinting ──────────────────────────────────────────

_INDICATOR_FAMILIES = {
    "rsi_momentum": "oscillator", "williams_r": "oscillator", "stochastic": "oscillator",
    "ema_cross": "moving_average", "hma_cross": "moving_average",
    "bollinger": "volatility_band", "keltner": "volatility_band",
    "macd": "momentum", "adx_trend": "trend_strength",
    "supertrend": "trend_following", "parabolic_sar": "trend_following",
    "ichimoku": "multi_indicator", "aroon": "trend_timing",
    "funding": "market_microstructure", "funding_reversion": "market_microstructure",
    "vwap": "volume_price",
}

_REGIME_CLASSES = {
    "rsi_momentum": "mixed", "bollinger": "mean_reversion", "funding": "mean_reversion",
    "williams_r": "mean_reversion", "stochastic": "mean_reversion",
    "ema_cross": "trend_following", "macd": "trend_following",
    "keltner": "trend_following", "supertrend": "trend_following",
    "adx_trend": "trend_following", "aroon": "trend_following",
    "parabolic_sar": "trend_following", "ichimoku": "trend_following",
    "hma_cross": "trend_following", "vwap": "mean_reversion",
    "funding_reversion": "market_microstructure",
}


def compute_archetype_fingerprint(
    strategy_type: str,
    params: dict | None = None,
    metrics: dict | None = None,
) -> dict:
    """P3-3: Compute archetype fingerprint for a strategy.

    Fields: strategy_type, regime_class, indicator_family, risk_profile.
    """
    normalized_type = str(strategy_type or "").strip().lower().replace("-", "_")
    params = params if isinstance(params, dict) else {}
    metrics = metrics if isinstance(metrics, dict) else {}

    indicator_family = _INDICATOR_FAMILIES.get(normalized_type, "unknown")
    regime_class = _REGIME_CLASSES.get(normalized_type, "unknown")

    # Derive risk profile from params/metrics
    has_stop = "stop_loss_pct" in params or "stop_loss" in params
    has_tp = "take_profit_pct" in params or "take_profit" in params
    max_dd = _to_float(metrics.get("max_drawdown_pct"), 0.0)

    if max_dd > 0.20:
        risk_profile = "aggressive"
    elif has_stop and has_tp:
        risk_profile = "managed"
    elif has_stop:
        risk_profile = "defensive"
    else:
        risk_profile = "unmanaged"

    return {
        "strategy_type": normalized_type,
        "regime_class": regime_class,
        "indicator_family": indicator_family,
        "risk_profile": risk_profile,
    }


def persist_archetype_fingerprint(strategy_id: str, fingerprint: dict):
    """Save fingerprint to strategy record."""
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET archetype_fingerprint = ? WHERE id = ?",
                (json.dumps(fingerprint), strategy_id),
            )
    except Exception:
        pass


def is_near_duplicate(
    fingerprint: dict,
    params: dict | None = None,
    active_stages: tuple[str, ...] = ("quick_screen", "gauntlet", "paper", "live_graduated"),
    similarity_threshold: float = 0.85,
) -> tuple[bool, str | None]:
    """P3-4: Check if a strategy fingerprint is near-duplicate of an active strategy.

    Compares fingerprint fields + parameter similarity to existing active strategies.
    Returns (is_duplicate, matching_strategy_id).
    """
    params = params if isinstance(params, dict) else {}
    try:
        with get_db() as conn:
            placeholders = ",".join("?" for _ in active_stages)
            rows = conn.execute(
                f"SELECT id, archetype_fingerprint, params FROM strategies WHERE stage IN ({placeholders})",
                active_stages,
            ).fetchall()

        for row in rows:
            existing_fp = {}
            try:
                existing_fp = json.loads(row["archetype_fingerprint"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not existing_fp:
                continue

            # Fingerprint similarity: count matching fields
            fp_fields = ["strategy_type", "regime_class", "indicator_family", "risk_profile"]
            matches = sum(
                1 for f in fp_fields
                if fingerprint.get(f) and fingerprint.get(f) == existing_fp.get(f)
            )
            fp_similarity = matches / len(fp_fields)

            if fp_similarity < similarity_threshold:
                continue

            # Parameter similarity check (for same-type strategies)
            if fingerprint.get("strategy_type") == existing_fp.get("strategy_type") and params:
                try:
                    existing_params = json.loads(row["params"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    existing_params = {}
                if existing_params:
                    all_keys = set(params.keys()) | set(existing_params.keys())
                    param_matches = sum(
                        1 for k in all_keys
                        if params.get(k) == existing_params.get(k)
                    )
                    param_similarity = param_matches / max(len(all_keys), 1)
                    if param_similarity > 0.80:
                        return True, row["id"]

            if fp_similarity >= 1.0:  # Exact fingerprint match
                return True, row["id"]

    except Exception:
        pass

    return False, None


def _parse_timeframe_minutes(value: str | None) -> int:
    raw = str(value or "").strip()
    match = re.fullmatch(r"(\d+)([mhdwM])", raw)
    if not match:
        return 60
    qty = max(int(match.group(1)), 1)
    unit = match.group(2)
    multipliers = {
        "m": 1,
        "h": 60,
        "d": 1440,
        "w": 10080,
        "M": 43200,
    }
    return qty * int(multipliers.get(unit, 60))


def _normalize_timeframe(value: str | None, fallback: str = "1h") -> str:
    candidate = str(value or "").strip()
    if re.fullmatch(r"\d+[mhdwM]", candidate):
        return candidate
    return fallback


# Per-context compute bounds for the evolution/crucible validation matrix (N backtests
# across symbols x timeframes, run via a thread pool). The MAX fits ~3.4y @1h so the
# common multi-year windows pass un-truncated; fine timeframes (5m/1m) clamp here so a
# single context can't request ~1M bars. The COARSE floor keeps 1d+ strategies tradeable.
_VALIDATION_MIN_BARS = 200
_VALIDATION_MAX_BARS = 30_000
_VALIDATION_COARSE_FLOOR_BARS = 1095


def _bars_for_validation_timeframe(timeframe: str) -> int:
    minutes_per_bar = max(_parse_timeframe_minutes(timeframe), 1)
    # Evolution/crucible discovery has its OWN per-stage window knob
    # (evolution_duration_days), which falls back to the global Default backtest window
    # when left at 0 — so the validation matrix can run a different horizon than the
    # gauntlet if the operator wants. Falls back to the canonical default if unreadable.
    try:
        from forven.api_core import stage_backtest_duration_days

        duration_days = stage_backtest_duration_days("evolution")
    except Exception:
        duration_days = 730
    duration_days = max(1, duration_days)
    raw_bars = int(round((duration_days * 24 * 60) / minutes_per_bar))
    bars = raw_bars
    # Coarse timeframes (>= 12h/bar, i.e. 1d and up) get too few bars in a short
    # window: a daily strategy lands ~1 bar/day, and after the backtester's ~210-bar
    # warmup floor and 70/30 in-sample split that can leave <50 usable IS bars —
    # manufacturing false "zero trades" archivals. Hold a ~3y floor for coarse
    # timeframes so the in-sample window is actually tradeable.
    if minutes_per_bar >= 12 * 60:
        bars = max(bars, _VALIDATION_COARSE_FLOOR_BARS)
    bars = max(_VALIDATION_MIN_BARS, min(_VALIDATION_MAX_BARS, bars))
    # Surface the compute clamp instead of silently shrinking the window — the validation
    # matrix would otherwise evaluate over a different horizon than the baseline backtest
    # (which honors the full window) with no operator visibility.
    if raw_bars > _VALIDATION_MAX_BARS:
        log.warning(
            "evolution validation window truncated for %s: %d bars (%dd configured) -> %d "
            "(per-context compute cap; the gauntlet evaluation still uses the full window)",
            timeframe, raw_bars, duration_days, _VALIDATION_MAX_BARS,
        )
    return bars


def _collect_validation_symbols(primary_symbol: str, params: dict | None = None) -> list[str]:
    symbols: list[str] = []

    def _append(candidate: object):
        value = str(candidate or "").strip().upper()
        if not value or value == "GENERIC":
            return
        if value not in symbols:
            symbols.append(value)

    _append(primary_symbol)
    payload = params if isinstance(params, dict) else {}
    for key in ("_asset", "asset", "symbol", "pair"):
        _append(payload.get(key))
    assets = payload.get("assets")
    if isinstance(assets, list):
        for item in assets[:8]:
            _append(item)
    elif isinstance(assets, str):
        _append(assets)

    raw_settings = kv_get("forven:settings", {})
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    _append(settings.get("backtest_symbol"))

    raw_pipeline = kv_get("forven:pipeline:settings", {})
    pipeline_settings = raw_pipeline if isinstance(raw_pipeline, dict) else {}
    _append(pipeline_settings.get("autopilot_scan_symbol"))
    configured_symbols = pipeline_settings.get("autopilot_scan_symbols")
    if isinstance(configured_symbols, list):
        for item in configured_symbols[:8]:
            _append(item)

    if not symbols:
        symbols.append("BTC/USDT")
    return symbols[:6]


def _collect_validation_timeframes(primary_timeframe: str | None = None) -> list[str]:
    timeframes: list[str] = []

    def _append(candidate: object):
        normalized = _normalize_timeframe(str(candidate or "").strip(), "")
        if not normalized:
            return
        if normalized not in timeframes:
            timeframes.append(normalized)

    _append(primary_timeframe or "1h")

    raw_settings = kv_get("forven:settings", {})
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    _append(settings.get("backtest_timeframe"))

    raw_pipeline = kv_get("forven:pipeline:settings", {})
    pipeline_settings = raw_pipeline if isinstance(raw_pipeline, dict) else {}
    _append(pipeline_settings.get("autopilot_scan_timeframe"))
    configured_timeframes = pipeline_settings.get("autopilot_scan_timeframes")
    if isinstance(configured_timeframes, list):
        for item in configured_timeframes[:8]:
            _append(item)

    for fallback in ("1h", "4h", "1d"):
        _append(fallback)

    return timeframes[:6]


def _build_validation_contexts(symbol: str, timeframe: str, params: dict | None = None) -> list[tuple[str, str]]:
    symbols = _collect_validation_symbols(symbol, params=params)
    timeframes = _collect_validation_timeframes(primary_timeframe=timeframe)
    contexts: list[tuple[str, str]] = []
    for sym in symbols:
        for tf in timeframes:
            contexts.append((sym, tf))

    raw_settings = kv_get("forven:settings", {})
    settings = raw_settings if isinstance(raw_settings, dict) else {}
    max_contexts = _coerce_int(settings.get("validation_max_contexts"), 12, 1, 36)
    return contexts[:max_contexts]


def _is_better_validation_candidate(
    candidate_fitness: float,
    candidate_metrics: dict,
    best_fitness: float,
    best_metrics: dict,
) -> bool:
    if candidate_fitness > best_fitness:
        return True
    if candidate_fitness < best_fitness:
        return False
    candidate_sharpe = _to_float(candidate_metrics.get("sharpe"), 0.0)
    best_sharpe = _to_float(best_metrics.get("sharpe"), 0.0)
    if candidate_sharpe > best_sharpe:
        return True
    if candidate_sharpe < best_sharpe:
        return False
    candidate_ret = _to_float(candidate_metrics.get("total_return_pct"), 0.0)
    best_ret = _to_float(best_metrics.get("total_return_pct"), 0.0)
    return candidate_ret > best_ret


def _attempt_stage_promotion(
    strategy_id: str,
    from_stage: str,
    to_stage: str,
    reason: str,
    *,
    record_failure_event: bool = True,
) -> tuple[bool, str]:
    """Evaluate and apply a stage transition when eligible."""
    passed, gate_reason = evaluate_promotion(strategy_id, from_stage, to_stage)
    if not passed:
        if record_failure_event:
            append_strategy_event(
                strategy_id=strategy_id,
                from_state=from_stage,
                to_state=from_stage,
                actor="system",
                reason=f"Gate failure: {gate_reason}",
                details={"requested_stage": to_stage, "motion": "gate_failure"},
            )
        return False, gate_reason

    transition = transition_stage(
        strategy_id,
        to_stage,
        reason=reason,
        actor="system",
    )
    normalized_target = normalize_stage(to_stage) or str(to_stage).strip().lower()
    actual_target = normalize_stage(transition.get("to")) or str(transition.get("to") or "").strip().lower()
    if actual_target != normalized_target:
        blocked_reason = str(
            transition.get("blocked_reason")
            or f"Transition blocked; strategy remains in {actual_target or from_stage}"
        ).strip()
        return False, blocked_reason or "transition blocked"
    return True, "promoted"


def _compute_and_persist_robustness_score(strategy_id: str) -> float:
    """Recompute and persist the composite robustness score.

    Delegates to the single canonical, verdict-honoring scorer in
    forven.routers.robustness (`_recalculate_robustness_score`) so there is exactly
    ONE scoring formula and one set of metric keys feeding the promotion gate. The
    previous evolution-local scorer awarded partial credit (up to 20 pts) to tests
    whose own verdict was FAIL — it only skipped a test on status=='failed'/'error',
    never on the verdict — which inflated the gating composite. Returns the resulting
    0–100 composite (0.0 if it could not be computed).
    """
    from forven.routers.robustness import _recalculate_robustness_score

    _recalculate_robustness_score(strategy_id)
    with get_db() as conn:
        row = conn.execute("SELECT metrics FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
    metrics: dict = {}
    if row and row["metrics"]:
        try:
            parsed = json.loads(row["metrics"]) if isinstance(row["metrics"], str) else (row["metrics"] or {})
            metrics = parsed if isinstance(parsed, dict) else {}
        except Exception:
            metrics = {}
    try:
        return float(metrics.get("composite_robustness_score") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _execute_gauntlet_step(
    action: str,
    strategy_id: str,
    strategy_type: str,
    symbol: str,
    timeframe: str,
    params: dict,
    from_state: str = "gauntlet",
) -> dict:
    """Execute a single readiness step. Returns a result dict.

    Used by both gauntlet and paper-live readiness advancement.
    The ``from_state`` parameter controls the stage label in event logs.
    """
    # ── Step 1: Multi-TF Backtest Sweep ──────────────────────────────
    if action == "run_timeframe_sweep":
        from forven.api_core import _load_pipeline_settings_payload, post_backtest_submit, BacktestSubmitBody

        ps = _load_pipeline_settings_payload()
        sweep_tfs = ps.get("gate_sweep_timeframes", ["1h", "4h", "1d"])

        with get_db() as conn:
            # First, recover any previously auto-trashed sweep backtests so
            # the readiness check can see them.
            recovered = conn.execute(
                """UPDATE backtest_results SET deleted_at = NULL
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                     AND deleted_at IS NOT NULL
                     AND TRIM(COALESCE(deleted_at, '')) != ''""",
                (strategy_id,),
            ).rowcount
            if recovered:
                log.info("TF sweep: recovered %d trashed backtests for %s", recovered, strategy_id)

            # Now check which TFs already have live (non-deleted) results.
            existing = conn.execute(
                """SELECT DISTINCT LOWER(TRIM(timeframe)) AS tf
                   FROM backtest_results
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                     AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')""",
                (strategy_id,),
            ).fetchall()
        existing_tfs = {r["tf"] for r in existing}

        submitted = []
        for tf in sweep_tfs:
            if tf.lower() in existing_tfs:
                continue
            try:
                body = BacktestSubmitBody(
                    strategy_id=strategy_id,
                    symbol=symbol,
                    timeframe=tf,
                    params=params,
                )
                post_backtest_submit(body, skip_auto_trash=True)
                submitted.append(tf)
            except Exception as exc:
                log.warning("TF sweep submit failed for %s/%s: %s", strategy_id, tf, exc)

        return {"action": "run_timeframe_sweep", "submitted": submitted,
                "recovered": recovered,
                "detail": f"Submitted {len(submitted)} new backtests, recovered {recovered} trashed"}

    # ── Step 2: Optimization ─────────────────────────────────────────
    if action == "run_optimization":
        from forven.api_core import _persist_backtest_result_row
        from forven.strategies.optimizer import optimize_strategy

        try:
            opt_result = optimize_strategy(
                strategy_id=strategy_id,
                asset=symbol,
                strategy_type=strategy_type,
            )
            if isinstance(opt_result, dict) and not opt_result.get("error"):
                import uuid as _uuid

                _persist_backtest_result_row(
                    result_id=f"opt_{_uuid.uuid4().hex[:12]}",
                    strategy_id=strategy_id,
                    result_type="optimization",
                    symbol=symbol,
                    timeframe=timeframe,
                    start_date=None,
                    end_date=None,
                    metrics=opt_result.get("best_metrics", {}),
                    config={
                        "status": "succeeded",
                        "best_params": opt_result.get("best_params", {}),
                        "best_fitness": opt_result.get("best_fitness"),
                    },
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                return {"action": "run_optimization", "detail": "Optimization completed",
                        "best_params": opt_result.get("best_params"),
                        "best_fitness": opt_result.get("best_fitness")}
            else:
                return {"action": "run_optimization", "detail": f"Optimization failed: {opt_result.get('error', 'unknown')}",
                        "error": True}
        except Exception as exc:
            log.warning("Optimization failed for %s: %s", strategy_id, exc)
            return {"action": "run_optimization", "detail": f"Optimization error: {exc}", "error": True}

    # ── Step 3: Apply Best Params ────────────────────────────────────
    if action == "apply_best_params":
        with get_db() as conn:
            opt_row = conn.execute(
                """SELECT metrics_json, config_json FROM backtest_results
                   WHERE strategy_id = ?
                      AND LOWER(TRIM(COALESCE(result_type, ''))) = 'optimization'
                      AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                    ORDER BY created_at DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()

        if not opt_row:
            return {"action": "apply_best_params", "detail": "No optimization result found", "error": True}

        try:
            config_payload = json.loads(opt_row["config_json"]) if isinstance(opt_row["config_json"], str) else (opt_row["config_json"] or {})
        except Exception:
            config_payload = {}
        try:
            metrics_payload = json.loads(opt_row["metrics_json"]) if isinstance(opt_row["metrics_json"], str) else (opt_row["metrics_json"] or {})
        except Exception:
            metrics_payload = {}

        best_params = {}
        for payload in (config_payload, metrics_payload):
            if not isinstance(payload, dict):
                continue
            for key in ("best_params", "params"):
                candidate = payload.get(key)
                if isinstance(candidate, dict) and candidate:
                    best_params = dict(candidate)
                    break
            if best_params:
                break

        if not best_params:
            return {"action": "apply_best_params", "detail": "Optimization has empty params", "error": True}

        # Operator-owned (paper/live) strategies have FROZEN default params. This
        # step is automated (actor='system'), so the write is refused — return a
        # benign no-op so the readiness loop does not treat it as a failure.
        from forven.brain import stage_is_param_locked

        if stage_is_param_locked(from_state):
            log.info(
                "params locked: strategy %s at stage %s; apply_best_params skipped",
                strategy_id, str(from_state or "").strip().lower(),
            )
            return {"action": "apply_best_params", "detail": "strategy is operator-owned (paper/live); apply_best_params skipped"}

        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET params = ?, updated_at = ? WHERE id = ?",
                (json.dumps(best_params), datetime.now(timezone.utc).isoformat(), strategy_id),
            )
        append_strategy_event(
            strategy_id=strategy_id,
            from_state=from_state,
            to_state=from_state,
            actor="system",
            reason="Applied optimized params from latest optimization run",
            details={"params": best_params, "motion": "params_applied"},
        )
        return {"action": "apply_best_params", "detail": "Applied best params from optimization"}

    # ── Step 4: Confirmation Backtest ────────────────────────────────
    if action == "run_confirmation_backtest":
        from forven.api_core import post_backtest_submit, BacktestSubmitBody

        # For an operator-owned (paper/live) strategy the confirmation backtest is
        # wasted compute: its result can no longer mutate the frozen params/metrics
        # (Layers 1d + 2a neuter the apply + metric-sync). Skip submission entirely.
        from forven.brain import stage_is_param_locked

        if stage_is_param_locked(from_state):
            log.info(
                "params locked: strategy %s at stage %s; confirmation backtest skipped",
                strategy_id, str(from_state or "").strip().lower(),
            )
            return {"action": "run_confirmation_backtest", "detail": "strategy is operator-owned (paper/live); confirmation backtest skipped"}

        with get_db() as conn:
            strat_row = conn.execute(
                "SELECT params FROM strategies WHERE id = ?", (strategy_id,),
            ).fetchone()
        current_params = {}
        if strat_row:
            try:
                current_params = json.loads(strat_row["params"]) if isinstance(strat_row["params"], str) else (strat_row["params"] or {})
            except Exception:
                current_params = {}

        try:
            body = BacktestSubmitBody(
                strategy_id=strategy_id,
                symbol=symbol,
                timeframe=timeframe,
                params=current_params,
            )
            post_backtest_submit(body, skip_auto_trash=True)
            return {"action": "run_confirmation_backtest", "detail": "Confirmation backtest submitted"}
        except Exception as exc:
            log.warning("Confirmation backtest failed for %s: %s", strategy_id, exc)
            return {"action": "run_confirmation_backtest", "detail": f"Backtest error: {exc}", "error": True}

    # ── Step 5: Validation Suite (WFA, MC, Jitter, Cost, Regime) ─────
    if action in ("run_validation_suite", "re_run_validation_suite"):
        with get_db() as conn:
            bt_row = conn.execute(
                """SELECT result_id, symbol, timeframe, start_date, end_date
                   FROM backtest_results
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                     AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                   ORDER BY created_at DESC LIMIT 1""",
                (strategy_id,),
            ).fetchone()

        if not bt_row:
            # Auto-submit a backtest so the next cycle can validate against it
            log.info("Gauntlet %s: no backtest found — auto-submitting confirmation backtest before validation", strategy_id)
            try:
                from forven.api_core import post_backtest_submit, BacktestSubmitBody
                with get_db() as conn:
                    strat_row = conn.execute(
                        "SELECT params, symbol, timeframe FROM strategies WHERE id = ?", (strategy_id,),
                    ).fetchone()
                bt_params = {}
                bt_symbol = symbol
                bt_timeframe = timeframe
                if strat_row:
                    try:
                        bt_params = json.loads(strat_row["params"]) if isinstance(strat_row["params"], str) else (strat_row["params"] or {})
                    except Exception:
                        bt_params = {}
                    bt_symbol = str(strat_row["symbol"] or symbol or "BTC/USDT").strip() or "BTC/USDT"
                    bt_timeframe = str(strat_row["timeframe"] or timeframe or "1h").strip() or "1h"
                body = BacktestSubmitBody(
                    strategy_id=strategy_id,
                    symbol=bt_symbol,
                    timeframe=bt_timeframe,
                    params=bt_params,
                )
                post_backtest_submit(body, skip_auto_trash=True)
                return {"action": "run_confirmation_backtest", "detail": "Auto-submitted missing backtest — validation will proceed next cycle"}
            except Exception as exc:
                log.warning("Auto-backtest submission failed for %s: %s", strategy_id, exc)
                return {"action": action, "detail": f"No backtest result and auto-submit failed: {exc}", "error": True}

        result_id = str(bt_row["result_id"])
        symbol = str(bt_row["symbol"] or "").strip()
        timeframe = str(bt_row["timeframe"] or "1h").strip() or "1h"
        start_date = str(bt_row["start_date"] or "").strip() or None
        end_date = str(bt_row["end_date"] or "").strip() or None

        from forven.routers.robustness import (
            CostStressBody,
            MonteCarloBody,
            ParamJitterBody,
            RegimeSplitBody,
            WalkForwardBody,
            submit_cost_stress,
            submit_monte_carlo,
            submit_param_jitter,
            submit_regime_split,
            submit_walk_forward,
        )

        robustness_results: dict[str, object] = {}
        submissions = [
            (
                "walk_forward",
                WalkForwardBody(
                    strategy_id=strategy_id,
                    symbol=symbol or "",
                    timeframe=timeframe,
                    start_date=start_date,
                    end_date=end_date,
                ),
                submit_walk_forward,
            ),
            ("monte_carlo", MonteCarloBody(result_id=result_id), submit_monte_carlo),
            (
                "param_jitter",
                ParamJitterBody(strategy_id=strategy_id, result_id=result_id),
                submit_param_jitter,
            ),
            (
                "cost_stress",
                CostStressBody(
                    strategy_id=strategy_id,
                    symbol=symbol or "",
                    timeframe=timeframe,
                    start_date=start_date,
                    end_date=end_date,
                ),
                submit_cost_stress,
            ),
            ("regime_split", RegimeSplitBody(result_id=result_id), submit_regime_split),
        ]
        for test_name, body, runner in submissions:
            try:
                if test_name in {"walk_forward", "cost_stress"} and not symbol:
                    raise ValueError("baseline backtest is missing symbol metadata")
                robustness_results[test_name] = runner(body)
            except Exception as exc:
                log.warning("Robustness test %s failed for %s: %s", test_name, strategy_id, exc)
                robustness_results[test_name] = f"error: {exc}"

        # NOTE: do NOT compute the composite score here. The validation tests above are
        # submitted ASYNCHRONOUSLY (they persist a 'running' placeholder and run on a
        # background executor), so the real artifacts are not written yet — an eager
        # recompute would read stale/missing rows. The canonical scorer
        # (_recalculate_robustness_score) runs as each test completes in the background,
        # keeping composite_robustness_score current once real artifacts land.

        return {"action": action, "detail": "Validation suite submitted", "robustness": robustness_results}

    # No automated action for this step
    log.info("Gauntlet readiness: no automated action for step %r on %s", action, strategy_id)
    return {"action": "none", "detail": f"Step {action} requires manual intervention",
            "step": action}


def _advance_gauntlet_readiness(
    strategy_id: str,
    strategy_type: str,
    symbol: str,
    timeframe: str,
    params: dict,
    drain: bool = False,
    deadline: float | None = None,
) -> dict:
    """Drive a gauntlet strategy through the promotion pipeline.

    When drain=False (legacy), advances one step and returns.
    When drain=True, loops through all steps until the strategy is ready,
    an error occurs, or the deadline is reached.

    Returns a dict describing what was done so the caller can track progress.
    """
    completed_steps: list[str] = []
    last_blocking_step: str | None = None  # detect no-progress loops

    while True:
        # Check time budget
        if drain and deadline is not None and time.monotonic() > deadline:
            return {"action": "drain_timeout", "detail": f"Drain budget expired after completing {len(completed_steps)} steps",
                    "completed_steps": completed_steps}

        readiness = check_promotion_readiness(strategy_id)
        if readiness.get("ready"):
            detail = "All readiness checks passed"
            if completed_steps:
                detail += f" (drained {len(completed_steps)} steps: {', '.join(completed_steps)})"
            return {"action": "ready", "detail": detail, "completed_steps": completed_steps}

        steps = readiness.get("steps", [])
        target_step = None
        for step in steps:
            if step["status"] in ("failed",):
                target_step = step
                break

        if target_step is None:
            detail = "No blocking failures remain"
            if completed_steps:
                detail += f" (drained {len(completed_steps)} steps: {', '.join(completed_steps)})"
            return {"action": "ready", "detail": detail, "completed_steps": completed_steps}

        action = target_step.get("actionable")
        step_name = target_step.get("name", "unknown")

        # ── No-progress guard: if we just executed this same step and
        #    it's still the first failing step, we're stuck in a loop.
        if drain and last_blocking_step == step_name and completed_steps and completed_steps[-1] == action:
            log.warning(
                "Gauntlet drain: step %r for %s succeeded but made no progress — breaking out to avoid infinite loop",
                step_name, strategy_id,
            )
            return {"action": "no_progress", "step": step_name,
                    "detail": f"Step {step_name!r} succeeded but readiness unchanged — strategy may need manual review",
                    "completed_steps": completed_steps}

        last_blocking_step = step_name

        log.info(
            "Gauntlet readiness: advancing %s — step %r needs action %r",
            strategy_id, step_name, action,
        )

        # ── Execute the step ──────────────────────────────────────────
        step_result = _execute_gauntlet_step(
            action=action,
            strategy_id=strategy_id,
            strategy_type=strategy_type,
            symbol=symbol,
            timeframe=timeframe,
            params=params,
        )

        # On error or non-automatable step, stop immediately
        if step_result.get("error") or step_result.get("action") == "none":
            step_result["completed_steps"] = completed_steps
            return step_result

        completed_steps.append(action)
        log.info(
            "Gauntlet readiness: completed step %r for %s (drain=%s, completed=%d)",
            action, strategy_id, drain, len(completed_steps),
        )

        # Without drain mode, return after one step (legacy behavior)
        if not drain:
            step_result["completed_steps"] = completed_steps
            return step_result

        # With drain mode, loop back to check for the next step
        continue


def _advance_paper_live_readiness(
    strategy_id: str,
    strategy_type: str,
    symbol: str,
    timeframe: str,
    params: dict,
    drain: bool = True,
    deadline: float | None = None,
) -> dict:
    """Drive optimization steps for a paper strategy preparing for live graduation.

    Only executes the actionable optimization steps (run_optimization,
    apply_best_params, run_confirmation_backtest).  Paper metric steps
    (duration, trades, return, drawdown) are not actionable and are skipped.

    Returns a dict describing what was done.
    """
    from forven.policy import check_paper_live_readiness

    completed_steps: list[str] = []
    last_blocking_step: str | None = None

    while True:
        if drain and deadline is not None and time.monotonic() > deadline:
            return {"action": "drain_timeout",
                    "detail": f"Paper-live drain budget expired after {len(completed_steps)} steps",
                    "completed_steps": completed_steps}

        readiness = check_paper_live_readiness(strategy_id)
        if readiness.get("ready"):
            detail = "All paper-live readiness checks passed"
            if completed_steps:
                detail += f" (completed {len(completed_steps)} steps: {', '.join(completed_steps)})"
            return {"action": "ready", "detail": detail, "completed_steps": completed_steps}

        steps = readiness.get("steps", [])
        target_step = None
        for step in steps:
            if step["status"] == "failed":
                # Paper metric steps are not actionable — if they fail, stop
                if step["name"] in ("paper_duration", "paper_trades", "paper_return", "paper_drawdown"):
                    return {"action": "paper_metrics_pending",
                            "detail": f"Paper metric not met: {step['detail']}",
                            "step": step["name"], "completed_steps": completed_steps}
                target_step = step
                break

        if target_step is None:
            return {"action": "ready", "detail": "No blocking failures remain",
                    "completed_steps": completed_steps}

        action = target_step.get("actionable")
        step_name = target_step.get("name", "unknown")

        if drain and last_blocking_step == step_name and completed_steps and completed_steps[-1] == action:
            log.warning(
                "Paper-live drain: step %r for %s made no progress — breaking",
                step_name, strategy_id,
            )
            return {"action": "no_progress", "step": step_name,
                    "detail": f"Step {step_name!r} succeeded but readiness unchanged",
                    "completed_steps": completed_steps}

        last_blocking_step = step_name
        log.info("Paper-live readiness: advancing %s — step %r action %r",
                 strategy_id, step_name, action)

        step_result = _execute_gauntlet_step(
            action=action,
            strategy_id=strategy_id,
            strategy_type=strategy_type,
            symbol=symbol,
            timeframe=timeframe,
            params=params,
            from_state="paper",
        )

        if step_result.get("error") or step_result.get("action") == "none":
            step_result["completed_steps"] = completed_steps
            return step_result

        completed_steps.append(action)
        log.info("Paper-live readiness: completed step %r for %s (completed=%d)",
                 action, strategy_id, len(completed_steps))

        if not drain:
            step_result["completed_steps"] = completed_steps
            return step_result

        continue


def _load_strategy_metrics(strategy_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT metrics FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
    if not row:
        return {}
    try:
        payload = json.loads(row["metrics"]) if isinstance(row["metrics"], str) else (row["metrics"] or {})
    except Exception:
        payload = {}
    return payload if isinstance(payload, dict) else {}


def _load_recent_execution_metrics(
    strategy_id: str,
    *,
    execution_pattern: str,
    lookback_hours: int,
) -> dict | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    with get_db() as conn:
        trade_rows = conn.execute(
            """
            SELECT pnl_pct
            FROM trades
            WHERE COALESCE(strategy_id, strategy) = ?
              AND status = 'CLOSED'
              AND pnl_pct IS NOT NULL
              AND LOWER(COALESCE(execution_type, '')) LIKE ?
              AND datetime(closed_at) >= datetime(?)
            """,
            (strategy_id, execution_pattern, cutoff),
        ).fetchall()

    pnls = [float(row["pnl_pct"]) for row in trade_rows if row["pnl_pct"] is not None]
    if not pnls:
        return None
    return compute_live_metrics(pnls)


def run_ideation_step():
    """Compatibility wrapper for the retired broad ideation cycle."""
    from forven.crucible_planner import run_crucible_planner_cycle

    log.info("Evolution: routing ideation step through crucible planner")
    result = run_crucible_planner_cycle(limit=3)
    log_activity("info", "evolution", f"Ideation step: delegated to crucible planner: {result}")
    return result


def _run_backtest_validation_sync(
    strategy_id: str,
    strategy_type: str,
    symbol: str,
    timeframe: str = "1h",
    bars: int | None = None,
    params: dict | None = None,
) -> dict:
    """Run a deterministic validation backtest using the local backtest engine."""
    from forven.strategies.backtest import backtest_strategy

    normalized_timeframe = _normalize_timeframe(timeframe, "1h")
    resolved_bars = int(bars) if bars else _bars_for_validation_timeframe(normalized_timeframe)
    resolved_params = params if isinstance(params, dict) else {}
    result = backtest_strategy(
        strategy_id=strategy_id,
        asset=symbol,
        strategy_type=strategy_type,
        params=resolved_params,
        bars=resolved_bars,
        timeframe=normalized_timeframe,
        leverage=float(resolved_params.get("leverage", 3.0)),
        regime_gate=True,  # P1-4: Match live regime gating in validation
        sync_strategy_state=False,
    )
    if isinstance(result, dict):
        return result
    return {"result": result}


def _backtest_matrix_workers() -> int:
    """Read configured parallel matrix worker count from settings."""
    raw = kv_get("forven:settings", {})
    settings = raw if isinstance(raw, dict) else {}
    return _coerce_int(settings.get("backtest_matrix_workers"), 4, 1, 8)


def _run_backtest_validation_matrix_sync(
    strategy_id: str,
    strategy_type: str,
    symbol: str,
    timeframe: str,
    params: dict | None = None,
) -> dict:
    contexts = _build_validation_contexts(symbol, timeframe, params=params)

    def _validate_one_context(ctx: tuple[str, str]) -> dict:
        """Run a single backtest context. Designed for thread pool execution."""
        candidate_symbol, candidate_timeframe = ctx
        candidate_bars = _bars_for_validation_timeframe(candidate_timeframe)
        try:
            result = _run_backtest_validation_sync(
                strategy_id=strategy_id,
                strategy_type=strategy_type,
                symbol=candidate_symbol,
                timeframe=candidate_timeframe,
                bars=candidate_bars,
                params=params,
            )
        except Exception as exc:
            return {
                "symbol": candidate_symbol, "timeframe": candidate_timeframe,
                "bars": candidate_bars, "fitness": 0.0, "error": str(exc),
            }

        if not isinstance(result, dict):
            return {
                "symbol": candidate_symbol, "timeframe": candidate_timeframe,
                "bars": candidate_bars, "fitness": 0.0, "error": "invalid_backtest_payload",
            }
        if result.get("error"):
            return {
                "symbol": candidate_symbol, "timeframe": candidate_timeframe,
                "bars": candidate_bars, "fitness": 0.0, "error": str(result.get("error")),
            }

        metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
        fitness = float(score_strategy(metrics))
        return {
            "symbol": candidate_symbol, "timeframe": candidate_timeframe,
            "bars": candidate_bars, "fitness": fitness, "metrics": metrics,
            "result": result, "error": None,
        }

    configured_workers = _backtest_matrix_workers()
    max_workers = max(1, min(configured_workers, len(contexts)))

    attempts: list[dict] = []
    best: dict | None = None

    if max_workers <= 1 or len(contexts) <= 1:
        # Sequential fallback — single worker or single context
        for ctx in contexts:
            entry = _validate_one_context(ctx)
            attempts.append({k: v for k, v in entry.items() if k not in ("result", "metrics")})
            if entry.get("error") is None:
                if best is None or _is_better_validation_candidate(
                    entry["fitness"], entry["metrics"],
                    float(best["fitness"]), best["metrics"],
                ):
                    best = entry
    else:
        # Parallel execution — each thread waits on a subprocess backtest
        log.info(
            "Matrix validation: %d contexts with %d parallel workers for %s",
            len(contexts), max_workers, strategy_id,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_ctx = {
                executor.submit(_validate_one_context, ctx): ctx
                for ctx in contexts
            }
            for future in concurrent.futures.as_completed(future_to_ctx):
                entry = future.result()
                attempts.append({k: v for k, v in entry.items() if k not in ("result", "metrics")})
                if entry.get("error") is None:
                    if best is None or _is_better_validation_candidate(
                        entry["fitness"], entry["metrics"],
                        float(best["fitness"]), best["metrics"],
                    ):
                        best = entry

    return {
        "contexts": attempts,
        "best": best,
    }


async def run_backtest_validation(
    strategy_id: str,
    strategy_type: str,
    symbol: str,
    timeframe: str = "1h",
    bars: int | None = None,
    params: dict | None = None,
) -> dict:
    """Async wrapper for deterministic backtest validation."""
    return await asyncio.to_thread(
        _run_backtest_validation_sync,
        strategy_id,
        strategy_type,
        symbol,
        timeframe,
        bars,
        params,
    )


async def run_simulation_validation(
    strategy_id: str,
    start_date: str,
    end_date: str,
    interval: str = "1h",
) -> dict:
    """Archived — simulation engine is disabled. Use backtest validation instead."""
    raise RuntimeError("Simulation engine is archived. Use run_backtest_validation() instead.")


def run_testing_step(code_first: bool = True) -> dict:
    """Run the testing step with a non-overlapping process guard.

    Scheduler timeouts can leave threadpool work running in the background.
    This lock prevents a second testing pass from starting until the first one
    fully exits.
    """
    global _TESTING_STEP_RUNNING_SINCE
    if not _TESTING_STEP_LOCK.acquire(blocking=False):
        running_for = None
        if _TESTING_STEP_RUNNING_SINCE is not None:
            running_for = round(max(time.monotonic() - _TESTING_STEP_RUNNING_SINCE, 0.0), 3)
        log.warning(
            "Evolution testing step skipped: previous invocation still running (running_for=%ss)",
            running_for if running_for is not None else "unknown",
        )
        return {
            "assigned": False,
            "assigned_count": 0,
            "validated": False,
            "validated_count": 0,
            "promoted": False,
            "promoted_count": 0,
            "archived": False,
            "archived_count": 0,
            "strategy_id": None,
            "task_id": None,
            "strategy_ids": [],
            "task_ids": [],
            "validation_results": [],
            "promoted_ids": [],
            "archived_ids": [],
            "gate_results": [],
            "candidate_count": 0,
            "reason": "testing_step_already_running",
            "running_for_seconds": running_for,
        }

    _TESTING_STEP_RUNNING_SINCE = time.monotonic()
    try:
        return _run_testing_step_impl(code_first=code_first)
    finally:
        _TESTING_STEP_RUNNING_SINCE = None
        _TESTING_STEP_LOCK.release()


_GAUNTLET_STALE_DAYS = 10
_GAUNTLET_ZERO_TRADE_DAYS = 2
_GAUNTLET_LOW_QUALITY_DAYS = 3
_GAUNTLET_LOW_TRADE_DAYS = 5
_QUICK_SCREEN_STALE_DAYS = 7
# Backstop for the "alive-looking but permanently un-promotable" blind spot: a
# gauntlet strategy with decent-looking metrics (positive sharpe, enough trades)
# that nonetheless keeps failing the gauntlet->paper gate (too few OOS trades,
# mis-ordered/stale validation artifacts, unloadable runtime) matches none of the
# quality rules above and churns forever. After this many days still failing the
# gate, archive it regardless of reason. Generous so the gauntlet workflow has
# ample time to legitimately promote or fail it first.
_GAUNTLET_UNPROMOTABLE_DAYS = 2.0

# Gauntlet-gate failures that mean "no validation evidence YET" rather than
# "tested and failed". A freshly-entered strategy legitimately fails these until
# the gauntlet workflow runs its validations, so the un-promotable backstop must
# NOT archive on them (that would kill strategies merely queued behind a backed-up
# pipeline). "Ordering violation" / stale-freshness / quality rejects all imply the
# artifacts DO exist, so those remain archivable.
_PENDING_EVIDENCE_GATE_MARKERS = (
    "requires at least one persisted",
    "no gauntlet metrics",
    "canonical backtest",
    "strategy not found",
)


def _is_pending_evidence_gate_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    return any(marker in text for marker in _PENDING_EVIDENCE_GATE_MARKERS)
# Strategies in these stages must have a loadable runtime: gauntlet needs it
# to backtest, paper needs it to evaluate live bars. A strategy whose runtime
# cannot load sits silently forever (it can never trade, so trade/duration
# gates never fire) — the S00785/S04585 paper-rot case.
_RUNTIME_HYGIENE_STAGES = ("gauntlet", "paper")

# Grace period before demotion, so a strategy isn't archived over a transient
# registry problem (e.g. a custom module mid-edit). Within the grace window the
# sweep only logs a warning.
_RUNTIME_UNLOADABLE_MIN_AGE_HOURS = 24.0


def _runtime_unloadable_reason(strategy_type: object, runtime_type: object) -> str | None:
    """Return why a strategy's runtime cannot load, or None if it resolves."""
    from forven.strategies.registry import runtime_unloadable_reason

    return runtime_unloadable_reason(strategy_type, runtime_type)


def _sweep_unloadable_runtimes(now: datetime) -> int:
    """Archive active-stage strategies whose runtime cannot load.

    Returns the number of strategies archived. Strategies inside the grace
    window are logged but left alone.
    """
    archived = 0
    try:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, stage, type, runtime_type, stage_changed_at
                   FROM strategies
                   WHERE LOWER(TRIM(stage)) IN ('gauntlet', 'paper')"""
            ).fetchall()
    except Exception as exc:
        log.warning("Runtime hygiene sweep could not list strategies: %s", exc)
        return 0

    for row in rows:
        reason = _runtime_unloadable_reason(row["type"], row["runtime_type"])
        if not reason:
            continue

        stage = str(row["stage"] or "").strip().lower()
        try:
            changed_at = datetime.fromisoformat(str(row["stage_changed_at"]))
            if changed_at.tzinfo is None:
                changed_at = changed_at.replace(tzinfo=timezone.utc)
            age_hours = (now - changed_at).total_seconds() / 3600
        except Exception:
            age_hours = 0.0

        if age_hours < _RUNTIME_UNLOADABLE_MIN_AGE_HOURS:
            log.warning(
                "Strategy %s in %s has an unloadable runtime (%s); demoting after %.0fh grace period",
                row["id"], stage, reason, _RUNTIME_UNLOADABLE_MIN_AGE_HOURS,
            )
            continue

        try:
            transition_stage(
                row["id"],
                "archived",
                reason=f"Pipeline hygiene: runtime unloadable in {stage}: {reason}",
                actor="pipeline_sweep",
                force=True,
            )
            archived += 1
            log_activity(
                "error",
                "pipeline",
                f"Archived {row['id']} from {stage}: runtime cannot load — {reason}",
            )
            log.warning("Runtime hygiene archived %s from %s: %s", row["id"], stage, reason)
        except Exception as exc:
            log.warning("Runtime hygiene failed to archive %s: %s", row["id"], exc)

    return archived


_SWEEP_COOLDOWN_KEY = "pipeline:last_hygiene_sweep"
_SWEEP_COOLDOWN_SECONDS = 300  # Don't sweep more than once per 5 min


def _sweep_pipeline_hygiene() -> dict[str, int]:
    """Archive obviously dead strategies in gauntlet and quick_screen.

    Runs at the start of each testing cycle to keep the pipeline flowing.
    Returns counts of archived strategies per stage.
    """
    # Cooldown — don't sweep every tick
    try:
        last_sweep = kv_get(_SWEEP_COOLDOWN_KEY)
        if last_sweep:
            from datetime import datetime, timezone
            last_dt = datetime.fromisoformat(str(last_sweep))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last_dt).total_seconds()
            if age < _SWEEP_COOLDOWN_SECONDS:
                return {"skipped": True}
    except Exception:
        pass

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    archived = {"gauntlet": 0, "quick_screen": 0}

    try:
        with get_db() as conn:
            rows = conn.execute(
                """SELECT s.id, s.stage, s.metrics, s.stage_changed_at, s.created_at,
                          (
                            SELECT se.reason
                            FROM strategy_events se
                            WHERE se.strategy_id = s.id
                              AND (
                                  se.reason LIKE 'Gate failure:%'
                                  OR se.reason LIKE '%gauntlet blocked: Gate%'
                                  OR se.reason LIKE 'Duplicate with active strategy%'
                              )
                              AND se.reason NOT LIKE '%canonical backtest%'
                            ORDER BY se.created_at DESC, se.id DESC
                            LIMIT 1
                          ) AS latest_gate_reason
                   FROM strategies s
                   WHERE LOWER(TRIM(s.stage)) IN ('gauntlet', 'quick_screen')"""
            ).fetchall()

        # Promotion-gate config + evaluator for the un-promotable backstop (Rule 6).
        # _evaluate_gauntlet_gate is the pure, read-only gauntlet->paper gate (no
        # slot-contention wrapper, no rejection-record side effects), so evaluating
        # it here neither logs nor archives slot-blocked challengers.
        from forven.policy import _evaluate_gauntlet_gate, load_pipeline_config
        pipeline_config = load_pipeline_config()

        for row in rows:
            strat_id = row["id"]
            stage = str(row["stage"]).strip().lower()
            metrics = {}
            try:
                metrics = json.loads(row["metrics"]) if row["metrics"] else {}
            except Exception:
                pass

            # Compute age in days
            try:
                changed_at = datetime.fromisoformat(row["stage_changed_at"])
                if changed_at.tzinfo is None:
                    changed_at = changed_at.replace(tzinfo=timezone.utc)
                age_days = (now - changed_at).total_seconds() / 86400
            except Exception:
                age_days = 0

            sharpe = float(metrics.get("sharpe_ratio") or metrics.get("sharpe") or 0)
            total_trades = int(metrics.get("total_trades") or 0)
            fitness = metrics.get("fitness")
            cagr = metrics.get("cagr") or metrics.get("total_return") or metrics.get("total_return_pct")

            reason = None

            if stage == "gauntlet":
                # Rule 1: Zero trades + stale → broken strategy
                if total_trades == 0 and age_days > _GAUNTLET_ZERO_TRADE_DAYS:
                    reason = f"Zero trades after {age_days:.0f}d in gauntlet (broken strategy)"

                # Rule 2: Negative sharpe with enough trades → proven loser
                elif sharpe < 0 and total_trades >= 5:
                    reason = f"Negative sharpe ({sharpe:.3f}) with {total_trades} trades"

                # Rule 3: Stale with no fitness → never completing pipeline
                elif age_days > _GAUNTLET_STALE_DAYS and fitness is None:
                    reason = f"No fitness after {age_days:.0f}d in gauntlet"

                # Rule 4: Below quality bar + stale
                elif sharpe < 0.3 and age_days > _GAUNTLET_LOW_QUALITY_DAYS and total_trades > 0:
                    reason = f"Low sharpe ({sharpe:.3f}) after {age_days:.0f}d in gauntlet"

                # Rule 5: Insufficient trades + stale
                elif total_trades < 10 and total_trades > 0 and age_days > _GAUNTLET_LOW_TRADE_DAYS:
                    reason = f"Only {total_trades} trades after {age_days:.0f}d in gauntlet"

                # Rule 6: Persistently un-promotable (the blind-spot backstop). A
                # strategy that LOOKS alive (positive sharpe, enough trades, fitness
                # set) escapes every rule above, yet may still be unable to clear the
                # gauntlet->paper gate — and the failure-counter archiver never fires
                # because routine promotion attempts are silent. Reason-agnostic and
                # churn-proof: re-promotion just restarts the grace clock, and a
                # strategy that becomes promotable leaves gauntlet on its own before
                # this fires. Slot-blocked challengers are NOT archived because the
                # pure gate evaluator passes them (slot contention lives in the
                # evaluate_promotion wrapper, which we deliberately bypass here).
                elif age_days > _GAUNTLET_UNPROMOTABLE_DAYS:
                    try:
                        _gate_ok, _gate_reason = _evaluate_gauntlet_gate(strat_id, pipeline_config)
                    except Exception as gate_exc:
                        _gate_ok, _gate_reason = True, f"gate eval errored: {gate_exc}"
                    # Only archive a genuinely TESTED-AND-FAILED strategy; a "no
                    # evidence yet" failure may just be a strategy queued behind a
                    # backed-up gauntlet workflow, so leave those for the workflow
                    # (and the >10d stale rule) instead.
                    if not _gate_ok and not _is_pending_evidence_gate_reason(_gate_reason):
                        reason = (
                            f"Un-promotable after {age_days:.0f}d in gauntlet "
                            f"(still fails paper gate: {_gate_reason})"
                        )

            elif stage == "quick_screen":
                latest_gate_reason = str(row["latest_gate_reason"] or "")

                # Rule 1: Hard terminal gate failures should free their crucible immediately.
                if _is_terminal_quick_screen_gate_failure(latest_gate_reason):
                    reason = f"Terminal gate failure: {_normalize_gate_failure_text(latest_gate_reason)}"

                # Rule 2: Stale untested
                elif age_days > _QUICK_SCREEN_STALE_DAYS and not metrics:
                    reason = f"Untested after {age_days:.0f}d in quick_screen"

                # Rule 3: Tested garbage
                elif total_trades > 0 and sharpe < 0 and cagr is not None and float(cagr) < 0:
                    reason = f"Negative sharpe ({sharpe:.3f}) and negative return"

                # Rule 4: Tested zero trades
                elif metrics and total_trades == 0 and age_days > _GAUNTLET_ZERO_TRADE_DAYS:
                    reason = "Zero trades after testing (broken strategy)"

            if reason:
                try:
                    transition_stage(
                        strat_id,
                        "archived",
                        reason=f"Pipeline hygiene: {reason}",
                        actor="pipeline_sweep",
                        force=True,  # bypass fitness guard — these are explicitly dead
                    )
                    archived[stage] = archived.get(stage, 0) + 1
                    log.info("Pipeline sweep archived %s: %s", strat_id, reason)
                except Exception as exc:
                    log.warning("Pipeline sweep failed to archive %s: %s", strat_id, exc)

        runtime_archived = _sweep_unloadable_runtimes(now)
        if runtime_archived:
            archived["unloadable_runtime"] = runtime_archived

        total = sum(archived.values())
        if total:
            log.warning(
                "Pipeline hygiene sweep archived %d strategies (gauntlet=%d, quick_screen=%d)",
                total, archived["gauntlet"], archived["quick_screen"],
            )
            try:
                from forven.notifications import emit_notification
                emit_notification(
                    "pipeline_hygiene",
                    severity="info",
                    source="evolution",
                    title="Pipeline hygiene sweep",
                    summary=f"Archived {total} dead strategies: {archived['gauntlet']} gauntlet, {archived['quick_screen']} quick_screen",
                    channel_name="alerts",
                    dedupe_key="pipeline_hygiene_sweep",
                )
            except Exception:
                pass

        kv_set(_SWEEP_COOLDOWN_KEY, now.isoformat())
    except Exception as exc:
        log.error("Pipeline hygiene sweep failed: %s", exc, exc_info=True)

    return archived


def _run_testing_step_impl(code_first: bool = True) -> dict:
    """Run quick_screen + gauntlet checks and route winners through the gauntlet.

    When pipeline_drain_mode is enabled (default), gauntlet strategies are
    driven through all readiness steps in a single call instead of advancing
    one step per scheduler tick.  A time budget (pipeline_drain_max_seconds,
    default 300s) prevents any single cycle from running indefinitely.
    """
    # Run hygiene sweep before processing to clear dead weight
    try:
        sweep_result = _sweep_pipeline_hygiene()
        if sweep_result.get("gauntlet") or sweep_result.get("quick_screen"):
            log.info("Pipeline sweep freed capacity: %s", sweep_result)
    except Exception as exc:
        log.warning("Pipeline hygiene sweep error: %s", exc)

    strategies = get_strategies()
    candidates = [
        s for s in strategies
        if _strategy_stage(s) in {"quick_screen", "gauntlet"}
        and _is_pipeline_candidate_strategy(s)
    ]
    throughput_plan = _resolve_pipeline_execution_plan(len(candidates))
    drain = bool(throughput_plan.get("drain"))
    drain_max = int(throughput_plan.get("drain_max_seconds") or _pipeline_drain_max_seconds())
    max_assignments = int(throughput_plan.get("max_assignments") or _pipeline_assignments_per_cycle())
    cycle_deadline = time.monotonic() + drain_max
    log.info(
        "Evolution: running testing step (adaptive=%s, target_clear_hours=%s, max_assignments=%s, drain=%s, budget=%ds)",
        bool(throughput_plan.get("adaptive")),
        int(throughput_plan.get("target_clear_hours") or 0),
        max_assignments,
        drain,
        drain_max,
    )

    outcome: dict[str, object] = {
        "assigned": False,
        "assigned_count": 0,
        "validated": False,
        "validated_count": 0,
        "promoted": False,
        "promoted_count": 0,
        "archived": False,
        "archived_count": 0,
        "strategy_id": None,
        "task_id": None,
        "strategy_ids": [],
        "task_ids": [],
        "validation_results": [],
        "promoted_ids": [],
        "archived_ids": [],
        "gate_results": [],
        "candidate_count": len(candidates),
        "reason": "",
    }

    if not candidates:
        log.info("Evolution: no strategies ready for quick_screen/gauntlet")
        outcome["reason"] = "no_testing_candidates"
        return outcome

    # Pick the first untested candidate
    for candidate in candidates:
        # Respect drain time budget across all strategies
        if drain and time.monotonic() > cycle_deadline:
            log.info("Evolution: drain budget expired, processed %d strategies this cycle",
                     int(outcome.get("assigned_count") or 0) + int(outcome.get("promoted_count") or 0))
            outcome["reason"] = outcome.get("reason") or "drain_budget_expired"
            break

        strat_id = candidate["id"]
        stage_name = _strategy_stage(candidate)
        strat_type = candidate.get("type", "")
        params = candidate.get("params", {})
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except (json.JSONDecodeError, TypeError):
                params = {}
        raw_symbol = str(candidate.get("symbol") or "").strip().upper()
        if not raw_symbol or raw_symbol == "GENERIC":
            inferred_symbol = ""
            if isinstance(params, dict):
                inferred_symbol = str(
                    params.get("_asset")
                    or params.get("asset")
                    or params.get("symbol")
                    or ""
                ).strip().upper()
                if (not inferred_symbol or inferred_symbol == "GENERIC") and isinstance(params.get("assets"), list):
                    for item in params.get("assets"):
                        maybe_symbol = str(item or "").strip().upper()
                        if maybe_symbol and maybe_symbol != "GENERIC":
                            inferred_symbol = maybe_symbol
                            break
            symbol = inferred_symbol or "BTC/USDT"
        else:
            symbol = raw_symbol
        timeframe = _normalize_timeframe(
            str(candidate.get("timeframe") or (params.get("timeframe") if isinstance(params, dict) else "") or "1h").strip(),
            "1h",
        )

        # Fast path: if metrics already satisfy current gate, promote immediately.
        try:
            to_stage = "gauntlet" if stage_name == "quick_screen" else "paper"
            promoted, gate_reason = _attempt_stage_promotion(
                strat_id,
                from_stage=stage_name,
                to_stage=to_stage,
                reason=f"Auto-promoted after {stage_name} gate pass",
                record_failure_event=False,
            )
            gate_results = list(outcome.get("gate_results") or [])
            gate_results.append({"strategy_id": strat_id, "passed": promoted, "reason": gate_reason})
            outcome["gate_results"] = gate_results
            if promoted:
                promoted_count = int(outcome.get("promoted_count") or 0) + 1
                promoted_ids = list(outcome.get("promoted_ids") or [])
                promoted_ids.append(strat_id)
                outcome.update(
                    {
                        "assigned": True,
                        "promoted": True,
                        "promoted_count": promoted_count,
                        "strategy_id": outcome.get("strategy_id") or strat_id,
                        "promoted_ids": promoted_ids,
                        "reason": "promoted_existing_metrics",
                    }
                )
                log_activity("info", "evolution", f"Testing step: auto-promoted {strat_id} to {to_stage}")
                if promoted_count >= max_assignments:
                    break
                continue
        except Exception as exc:
            log.warning("Evolution promotion check failed for %s: %s", strat_id, exc)

        # ── Gauntlet pipeline orchestration ──────────────────────────
        # With drain mode, drive each strategy through ALL readiness steps
        # in one pass (up to the time budget).  Without drain, advance one
        # step and let the scheduler call us again later.
        if stage_name == "gauntlet":
            try:
                # Per-strategy deadline: min of 120s or remaining cycle budget.
                # This ensures one slow optimization doesn't starve the others.
                per_strategy_budget = 120  # seconds
                strategy_deadline = min(
                    time.monotonic() + per_strategy_budget,
                    cycle_deadline,
                )
                advance_result = _advance_gauntlet_readiness(
                    strategy_id=strat_id,
                    strategy_type=strat_type,
                    symbol=symbol,
                    timeframe=timeframe,
                    params=params,
                    drain=drain,
                    deadline=strategy_deadline,
                )
                advance_action = advance_result.get("action", "none")
                if advance_action == "ready":
                    # All readiness checks passed — try promotion again
                    promoted_now, gate_reason = _attempt_stage_promotion(
                        strat_id,
                        from_stage="gauntlet",
                        to_stage="paper",
                        reason="Auto-promoted after gauntlet readiness pipeline completed",
                    )
                    if promoted_now:
                        promoted_count = int(outcome.get("promoted_count") or 0) + 1
                        promoted_ids = list(outcome.get("promoted_ids") or [])
                        promoted_ids.append(strat_id)
                        outcome.update({
                            "assigned": True,
                            "promoted": True,
                            "promoted_count": promoted_count,
                            "strategy_id": outcome.get("strategy_id") or strat_id,
                            "promoted_ids": promoted_ids,
                            "reason": "promoted_after_readiness_pipeline",
                        })
                        log_activity("info", "evolution", f"Testing step: promoted {strat_id} after readiness pipeline")
                elif not advance_result.get("error"):
                    log_activity(
                        "info", "evolution",
                        f"Gauntlet pipeline: {strat_id} advanced step={advance_action} — {advance_result.get('detail', '')}",
                    )
                    outcome.update({
                        "assigned": True,
                        "assigned_count": int(outcome.get("assigned_count") or 0) + 1,
                        "strategy_id": outcome.get("strategy_id") or strat_id,
                        "reason": f"gauntlet_readiness_{advance_action}",
                    })
                else:
                    log.warning(
                        "Gauntlet readiness step failed for %s: %s",
                        strat_id, advance_result.get("detail"),
                    )
                completed_work = int(outcome.get("assigned_count") or 0) + int(outcome.get("promoted_count") or 0)
                if completed_work >= max_assignments:
                    break
                continue
            except Exception as exc:
                log.warning("Gauntlet readiness orchestration failed for %s: %s", strat_id, exc)
                # Fall through to existing code-first / agent-assignment path

        if code_first:
            try:
                validation_matrix = _run_backtest_validation_matrix_sync(
                    strategy_id=strat_id,
                    strategy_type=strat_type,
                    symbol=symbol,
                    timeframe=timeframe,
                    params=params,
                )
                best_validation = validation_matrix.get("best") if isinstance(validation_matrix, dict) else None
                if not isinstance(best_validation, dict):
                    contexts = (
                        validation_matrix.get("contexts")
                        if isinstance(validation_matrix, dict)
                        else []
                    )
                    failures = [c for c in contexts if isinstance(c, dict) and c.get("error")]
                    first_failure = failures[0]["error"] if failures else "no_valid_validation_contexts"
                    raise RuntimeError(str(first_failure))

                selected_symbol = str(best_validation.get("symbol") or symbol).strip().upper() or symbol
                selected_timeframe = _normalize_timeframe(str(best_validation.get("timeframe") or timeframe).strip(), timeframe)
                metrics = best_validation.get("metrics") if isinstance(best_validation.get("metrics"), dict) else {}
                context_fitness = _to_float(best_validation.get("fitness"), 0.0)
                stored_metrics = _load_strategy_metrics(strat_id)
                merged_metrics = dict(stored_metrics)
                merged_metrics.update(metrics)
                merged_metrics["fitness"] = context_fitness
                updated_name = build_strategy_container_name(
                    symbol=selected_symbol,
                    type_=strat_type,
                    strategy_id=strat_id,
                )
                with get_db() as conn:
                    conn.execute(
                        "UPDATE strategies SET metrics = ?, symbol = ?, timeframe = ?, name = ?, updated_at = ? WHERE id = ?",
                        (
                            json.dumps(merged_metrics),
                            selected_symbol,
                            selected_timeframe,
                            updated_name,
                            datetime.now(timezone.utc).isoformat(),
                            strat_id,
                        ),
                    )

                promoted_now = False
                gate_reason = "No gate evaluation recorded"
                try:
                    to_stage = "gauntlet" if stage_name == "quick_screen" else "paper"
                    promoted_now, gate_reason = _attempt_stage_promotion(
                        strat_id,
                        from_stage=stage_name,
                        to_stage=to_stage,
                        reason=f"Auto-promoted after {stage_name} validation",
                    )
                except Exception as exc:
                    gate_reason = f"Promotion error: {exc}"
                    log.warning("Evolution post-validation promotion failed for %s: %s", strat_id, exc)

                gate_results = list(outcome.get("gate_results") or [])
                gate_results.append({"strategy_id": strat_id, "passed": promoted_now, "reason": gate_reason})

                validated_count = int(outcome.get("validated_count") or 0) + 1
                strategy_ids = list(outcome.get("strategy_ids") or [])
                strategy_ids.append(strat_id)
                validation_results = list(outcome.get("validation_results") or [])
                validation_results.append(
                    {
                        "strategy_id": strat_id,
                        "strategy_type": strat_type,
                        "symbol": selected_symbol,
                        "timeframe": selected_timeframe,
                        "fitness": context_fitness,
                        "metrics": merged_metrics,
                        "verdict_status": None,
                        "contexts": validation_matrix.get("contexts") if isinstance(validation_matrix, dict) else [],
                    }
                )
                outcome.update(
                    {
                        "assigned": True,
                        "validated": True,
                        "assigned_count": validated_count,
                        "validated_count": validated_count,
                        "strategy_id": outcome.get("strategy_id") or strat_id,
                        "strategy_ids": strategy_ids,
                        "validation_results": validation_results,
                        "gate_results": gate_results,
                        "reason": "validated_code_first",
                    }
                )
                if promoted_now:
                    promoted_count = int(outcome.get("promoted_count") or 0) + 1
                    promoted_ids = list(outcome.get("promoted_ids") or [])
                    promoted_ids.append(strat_id)
                    outcome.update(
                        {
                            "promoted": True,
                            "promoted_count": promoted_count,
                            "promoted_ids": promoted_ids,
                            "reason": "validated_and_promoted",
                        }
                    )
                    log_activity("info", "evolution", f"Testing step: validated and promoted {strat_id}")
                elif stage_name in {"quick_screen", "gauntlet"} and not str(gate_reason).lower().startswith("no "):
                    if (
                        stage_name == "quick_screen"
                        and _is_terminal_quick_screen_gate_failure(gate_reason)
                    ):
                        try:
                            if _archive_terminal_quick_screen_gate_failure(strat_id, gate_reason):
                                archived_count = int(outcome.get("archived_count") or 0) + 1
                                archived_ids = list(outcome.get("archived_ids") or [])
                                archived_ids.append(strat_id)
                                outcome.update(
                                    {
                                        "archived": True,
                                        "archived_count": archived_count,
                                        "archived_ids": archived_ids,
                                        "reason": "terminal_quick_screen_gate_archived",
                                    }
                                )
                                log_activity(
                                    "info",
                                    "evolution",
                                    f"Testing step: archived terminal quick-screen reject {strat_id}",
                                )
                        except Exception as exc:
                            log.warning("Failed to archive terminal quick-screen reject %s: %s", strat_id, exc)
                    else:
                        # Archive after configurable gate-failure attempts.
                        # Gauntlet gets a lower threshold (2) since retries rarely help.
                        try:
                            base_attempts = _pipeline_gate_failure_archive_attempts()
                            archive_attempts = min(2, base_attempts) if stage_name == "gauntlet" else base_attempts
                            with get_db() as _conn:
                                # Count DISTINCT calendar-day gate failures since the strategy
                                # last entered its current stage (stage_changed_at).
                                # Rules:
                                #   1. Only match genuine gate-block messages (not infrastructure
                                #      blocks like canonical-backtest guards).
                                #   2. Deduplicate: identical failure on the same UTC date counts
                                #      once — prevents rapid scheduler ticks from inflating the
                                #      counter and causing premature auto-archive.
                                #   3. Only count failures that occurred after the strategy was
                                #      placed in its current stage, so a revived strategy starts
                                #      fresh without inheriting historical failure counts.
                                _attempts_row = _conn.execute(
                                    """SELECT COUNT(DISTINCT DATE(se.created_at) || '|' || SUBSTR(se.reason, 1, 300)) AS cnt
                                       FROM strategy_events se
                                       JOIN strategies s ON s.id = se.strategy_id
                                       WHERE se.strategy_id = ?
                                         AND (
                                             se.reason LIKE 'Gate failure:%'
                                             OR se.reason LIKE '%gauntlet blocked: Gate%'
                                         )
                                         AND se.reason NOT LIKE '%canonical backtest%'
                                         AND se.created_at >= COALESCE(s.stage_changed_at, '1970-01-01')""",
                                    (strat_id,),
                                ).fetchone()
                                _attempt_count = int(_attempts_row["cnt"] if _attempts_row else 0)
                            if _attempt_count >= archive_attempts:
                                transition_stage(
                                    strat_id,
                                    "archived",
                                    reason=f"Auto-archived after {_attempt_count} gate failures (threshold: {archive_attempts}): {gate_reason}",
                                    actor="system",
                                )
                            else:
                                log.info(
                                    "Gate attempt %d/%d failed for %s: %s — keeping in %s",
                                    _attempt_count,
                                    archive_attempts,
                                    strat_id,
                                    gate_reason,
                                    stage_name,
                                )
                        except Exception as exc:
                            log.warning("Failed to evaluate gate attempts for %s: %s", strat_id, exc)
                log_activity("info", "evolution", f"Testing step: code-first validated {strat_id}")
                if validated_count >= max_assignments:
                    break
                continue
            except Exception as exc:
                log.warning("Code-first testing failed for %s (%s), falling back to agent assignment", strat_id, exc)

        # Load actual pipeline thresholds so the agent respects user settings
        from forven.policy import load_pipeline_config
        pipeline_cfg = load_pipeline_config()
        quick_screen = pipeline_cfg.get("quick_screen", {})
        gauntlet = pipeline_cfg.get("gauntlet", {})
        min_sharpe = quick_screen.get("min_sharpe", 1.0)
        max_dd_pct = quick_screen.get("max_drawdown_pct", 0.25) * 100
        min_return_pct = quick_screen.get("min_total_return_pct", 5.0)
        min_robustness = gauntlet.get("min_robustness_score", 70)
        candidate_symbols = _collect_validation_symbols(symbol, params=params)
        candidate_timeframes = _collect_validation_timeframes(primary_timeframe=timeframe)

        prompt = (
            f"TESTING CYCLE — Rigorously validate strategy {strat_id} ({strat_type} on {symbol}).\n\n"
            f"Parameters: {json.dumps(params)}\n\n"
            "Tasks:\n"
            "1. MANDATORY: Run 1-year baseline backtests across context grid.\n"
            f"   Symbols: {candidate_symbols}\n"
            f"   Timeframes: {candidate_timeframes}\n"
            f"   Use run_backtest with strategy_type={strat_type} and pick the best symbol/timeframe context by robustness + fitness.\n"
            f"2. If baseline Sharpe > 0.5, you MUST run Walk-Forward Analysis (WFA) via the `optimize_strategy` tool.\n"
            "3. If WFA passes without severe degradation, you MUST run the full robustness gauntlet and persist the actual artifacts for walk_forward, monte_carlo, param_jitter, cost_stress, and regime_split. Do not treat synthetic verdict summaries as promotion evidence.\n"
            "4. Record all baseline results and robustness artifacts in ChromaDB via store_chroma.\n"
            "5. Summarize: PASS (promote to paper) or FAIL (archive).\n\n"
            f"Quick Screen floor: Return >= {min_return_pct}%, Sharpe >= {min_sharpe}, MaxDD <= {max_dd_pct}%.\n"
            f"Gauntlet floor: Composite robustness >= {min_robustness}/100 plus persisted robustness artifacts for every required test."
        )

        task_id = assign_task(
            agent_id="simulation-agent",
            task_type="backtest",
            title=f"WFA: Validate {strat_id}",
            description=prompt,
            input_data={
                "strategy_id": strat_id,
                "strategy": strat_id,
                "strategy_type": strat_type,
                "symbol": symbol,
                "timeframe": timeframe,
                "candidate_symbols": candidate_symbols,
                "candidate_timeframes": candidate_timeframes,
            },
            strategy_id=strat_id,
        )

        confirmed_assignment = False
        assigned_task_id = None
        try:
            assigned_task_id = int(task_id)
        except Exception:
            assigned_task_id = None

        if assigned_task_id is not None:
            with get_db() as conn:
                task_row = conn.execute(
                    "SELECT status FROM agent_tasks WHERE id = ? AND strategy_id = ? "
                    "AND type = 'backtest' AND agent_id = 'simulation-agent'",
                    (assigned_task_id, strat_id),
                ).fetchone()
            status = str(task_row["status"] or "").strip().lower() if task_row else ""
            confirmed_assignment = status in {"pending", "running", "blocked"}

        if not confirmed_assignment:
            log.warning("Evolution testing assignment skipped for %s: assignment not confirmed", strat_id)
            outcome["reason"] = "assignment_not_confirmed"
            continue

        assigned_count = int(outcome.get("assigned_count") or 0) + 1
        strategy_ids = list(outcome.get("strategy_ids") or [])
        task_ids = list(outcome.get("task_ids") or [])
        strategy_ids.append(strat_id)
        task_ids.append(assigned_task_id)

        outcome.update(
            {
                "assigned": True,
                "assigned_count": assigned_count,
                "strategy_id": outcome.get("strategy_id") or strat_id,
                "task_id": outcome.get("task_id") or assigned_task_id,
                "strategy_ids": strategy_ids,
                "task_ids": task_ids,
                "reason": "assigned",
            }
        )
        log_activity("info", "evolution", f"Testing step: assigned {strat_id} to simulation-agent (task={assigned_task_id})")
        if assigned_count >= max_assignments:
            break

    if not outcome.get("assigned"):
        if not outcome.get("reason"):
            outcome["reason"] = "no_assignment_made"
        log.info("Evolution: testing step completed without assignment (%s)", outcome["reason"])

    # --- STALE CLEANUP RULES ---
    _run_stale_cleanup()

    # --- SUSTAINED GAUNTLET OVERFLOW ALERT ---
    _check_gauntlet_overflow_alert()

    return outcome


def _run_stale_cleanup():
    """Clean up stale containers: gauntlet without backtest >48h, quick_screen no activity >7d."""
    from forven.brain import transition_stage
    now = datetime.now(timezone.utc)
    cutoff_48h = (now - timedelta(hours=48)).isoformat()
    cutoff_7d = (now - timedelta(days=7)).isoformat()

    with get_db() as conn:
        # Gauntlet + no canonical backtest + stage_changed_at > 48h → demote to quick_screen
        stale_gauntlet = conn.execute(
            """
            SELECT s.id FROM strategies s
            WHERE LOWER(TRIM(s.stage)) = 'gauntlet'
              AND s.stage_changed_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM backtest_results br
                  LEFT JOIN backtest_result_trash bt ON bt.result_id = br.result_id
                  WHERE br.strategy_id = s.id
                    AND bt.result_id IS NULL
                    AND br.deleted_at IS NULL
              )
            """,
            (cutoff_48h,),
        ).fetchall()

        for row in stale_gauntlet:
            try:
                transition_stage(
                    strategy_id=row["id"],
                    target_stage="quick_screen",
                    reason="Stale gauntlet: no canonical backtest after 48h",
                    actor="system",
                )
                log.info("Stale cleanup: demoted %s from gauntlet (no backtest >48h)", row["id"])
            except Exception as exc:
                log.warning("Stale cleanup failed for %s: %s", row["id"], exc)

        # quick_screen + no strategy_events for 7 days → archive
        stale_qs = conn.execute(
            """
            SELECT s.id FROM strategies s
            WHERE LOWER(TRIM(s.stage)) = 'quick_screen'
              AND s.stage_changed_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM strategy_events se
                  WHERE se.strategy_id = s.id
                    AND se.created_at > ?
              )
            """,
            (cutoff_7d, cutoff_7d),
        ).fetchall()

    for row in stale_qs:
        try:
            transition_stage(
                strategy_id=row["id"],
                target_stage="archived",
                reason="Stale quick_screen: no activity for 7 days",
                actor="api",
                force=True,
            )
            log.info("Stale cleanup: archived %s from quick_screen (no activity >7d)", row["id"])
        except Exception as exc:
            log.warning("Stale cleanup failed for %s: %s", row["id"], exc)


def _check_gauntlet_overflow_alert():
    """Sustained gauntlet overflow alert: warn if gauntlet > 45 for 7+ consecutive days."""
    from forven.lab_features import GAUNTLET_MAX

    with get_db() as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) AS c FROM strategies WHERE LOWER(TRIM(stage)) = 'gauntlet'"
        ).fetchone()
    gauntlet_count = int(count_row["c"]) if count_row else 0
    alert_threshold = int(GAUNTLET_MAX * 0.9)  # 45

    alert_key = "forven:alert:gauntlet_overflow_start"
    if gauntlet_count > alert_threshold:
        start = kv_get(alert_key)
        if not start:
            kv_set(alert_key, datetime.now(timezone.utc).isoformat())
        else:
            try:
                start_dt = datetime.fromisoformat(start)
                if (datetime.now(timezone.utc) - start_dt).days >= 7:
                    log_activity(
                        "warning", "pipeline",
                        f"Sustained gauntlet overflow: {gauntlet_count} containers for 7+ days. "
                        "Consider reviewing brain quality filters.",
                    )
                    # Reset to avoid spam — fires once per episode
                    kv_set(alert_key, datetime.now(timezone.utc).isoformat())
            except Exception:
                pass
    else:
        # Clear alert when under threshold
        if kv_get(alert_key):
            kv_set(alert_key, None)


def run_coding_step():
    """Compatibility wrapper for the retired broad coding cycle."""
    from forven.crucible_planner import run_crucible_planner_cycle

    log.info("Evolution: routing coding step through crucible planner")
    result = run_crucible_planner_cycle(limit=3)
    log_activity("info", "evolution", f"Coding step: delegated to crucible planner: {result}")
    return result


def check_paper_graduation():
    """Step 3: Check if paper-trading strategies are ready for deployment.

    Two-phase check:
    1. If evaluate_promotion passes (paper metrics + optimization gates), graduate.
    2. If paper metrics are met but optimization is pending, drive optimization
       automatically via _advance_paper_live_readiness.
    """
    log.info("Evolution: checking paper graduation")

    strategies = get_strategies()
    paper_strategies = [s for s in strategies if _strategy_stage(s) == "paper"]

    if not paper_strategies:
        log.info("Evolution: no strategies in paper trading")
        return

    graduated = []
    needs_more_time = []
    optimizing = []

    for s in paper_strategies:
        strat_id = s["id"]
        passed, gate_reason = evaluate_promotion(strat_id, "paper", "live_graduated")

        if passed:
            transition_stage(strat_id, "live_graduated", reason="Paper graduation criteria met", actor="system")
            graduated.append(f"{strat_id}")
            log.info("Evolution: graduated %s to live_graduated", strat_id)
        else:
            # Check if paper metrics are met but optimization is pending
            from forven.policy import check_paper_live_readiness
            readiness = check_paper_live_readiness(strat_id)
            paper_metric_steps = ("paper_duration", "paper_trades", "paper_return", "paper_drawdown")
            paper_metrics_ok = all(
                step["status"] in ("passed", "skipped", "warning")
                for step in readiness.get("steps", [])
                if step["name"] in paper_metric_steps
            )
            if paper_metrics_ok and not readiness.get("ready"):
                # Paper metrics passed but optimization pending — drive it
                log.info("Evolution: %s paper metrics met, driving optimization", strat_id)
                strategy_type = s.get("strategy_type", s.get("type", ""))
                symbol = s.get("symbol", s.get("asset", ""))
                timeframe = s.get("timeframe", "1h")
                params = {}
                try:
                    params = json.loads(s["params"]) if isinstance(s.get("params"), str) else (s.get("params") or {})
                except Exception:
                    pass
                result = _advance_paper_live_readiness(
                    strategy_id=strat_id,
                    strategy_type=strategy_type,
                    symbol=symbol,
                    timeframe=timeframe,
                    params=params,
                    drain=True,
                    deadline=time.monotonic() + 600,  # 10 min budget per strategy
                )
                optimizing.append(strat_id)
                log.info("Evolution: paper-live readiness for %s: %s", strat_id, result.get("action"))
            else:
                needs_more_time.append(strat_id)
                log.info("Evolution: %s not ready — %s", strat_id, gate_reason)

    if graduated:
        log_activity("info", "evolution", f"Paper graduation: live_graduated {', '.join(graduated)}")
    if optimizing:
        log_activity("info", "evolution", f"Paper-live optimization triggered: {', '.join(optimizing)}")
    if needs_more_time:
        log.info("Evolution: still incubating: %s", ", ".join(needs_more_time))

    # Research recovery sweep
    _sweep_research_recovery()


def _sweep_research_recovery():
    """Periodic sweep: re-certify research_only strategies, promote up to 3 per cycle.

    Oldest first, Tier 1 failures first. Sequential (1 at a time).
    Also archives research_only strategies inactive > 30 days.
    """
    from forven.brain import try_research_recovery

    now = datetime.now(timezone.utc)
    cutoff_30d = (now - timedelta(days=30)).isoformat()

    with get_db() as conn:
        research_rows = conn.execute(
            """
            SELECT id, type, params, status_reason, stage_changed_at
            FROM strategies
            WHERE LOWER(TRIM(stage)) = 'research_only'
            ORDER BY created_at ASC
            """
        ).fetchall()

    if not research_rows:
        return

    # Archive 30-day inactive research_only
    for row in research_rows:
        sid = row["id"]
        changed_at = row["stage_changed_at"] or ""
        if changed_at and changed_at < cutoff_30d:
            # Check for any recent events
            with get_db() as conn:
                recent = conn.execute(
                    "SELECT 1 FROM strategy_events WHERE strategy_id = ? AND created_at > ? LIMIT 1",
                    (sid, cutoff_30d),
                ).fetchone()
            if not recent:
                try:
                    transition_stage(
                        strategy_id=sid,
                        target_stage="archived",
                        reason="Research-only: no activity for 30+ days",
                        actor="api",
                        force=True,
                    )
                    log.info("Research sweep: archived %s (30d inactive)", sid)
                except Exception as exc:
                    log.warning("Research sweep archive failed for %s: %s", sid, exc)

    # Sort by tier (Tier 1 first), then by created_at (oldest first)
    candidates = []
    for row in research_rows:
        status_reason = row["status_reason"] or ""
        if status_reason.startswith("tier"):
            try:
                tier = int(status_reason[4])
            except (IndexError, ValueError):
                tier = 2
        else:
            tier = 2
        candidates.append((tier, row))

    candidates.sort(key=lambda x: x[0])

    # Parse/compile check + re-certify, max 3 promotions
    promoted_count = 0
    for _tier, row in candidates:
        if promoted_count >= 3:
            break
        sid = row["id"]

        # Skip code_error — parse check
        status_reason = row["status_reason"] or ""
        if "code_error" in status_reason:
            continue

        try:
            result = try_research_recovery(sid)
            if result.get("promoted"):
                promoted_count += 1
                log.info("Research sweep: promoted %s to quick_screen", sid)
        except Exception as exc:
            log.warning("Research sweep re-cert failed for %s: %s", sid, exc)

    if promoted_count:
        log_activity("info", "evolution", f"Research recovery sweep: promoted {promoted_count} strategies")


def _brain_research_recovery_step():
    """Agent-driven research recovery: suggest param fixes for research_only strategies.

    Gated by ``brain_research_recovery_enabled()`` feature flag (default: False).
    Max 5 strategies per cycle, oldest first. All mutations logged to ``mutation_audit_log``.
    """
    from forven.lab_features import brain_research_recovery_enabled
    if not brain_research_recovery_enabled():
        return

    from forven.brain import try_research_recovery

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT id, type, params FROM strategies
            WHERE LOWER(TRIM(stage)) = 'research_only'
            ORDER BY created_at ASC
            LIMIT 5
            """
        ).fetchall()

    for row in rows:
        sid = row["id"]
        try:
            import json as _json
            params = _json.loads(row["params"]) if row["params"] else {}

            # Simple heuristic: no AI call needed, just validate current params
            # and try re-certification with them as-is
            result = try_research_recovery(sid)
            if result.get("promoted"):
                log.info("Brain research recovery: promoted %s", sid)
        except Exception as exc:
            log.warning("Brain research recovery failed for %s: %s", sid, exc)


def run_weekly_review():
    """Step 4/5: Weekly review — retire underperformers, reward top performers, assign post-mortems.

    Actions:
    - Retire bottom 20% of deployed strategies (fitness < threshold)
    - Assign post-mortem analysis for retired strategies
    - Log top performers in evolution journal
    """
    log.info("Evolution: running weekly review")

    strategies = get_strategies()
    deployed = [s for s in strategies if _strategy_stage(s) == "live_graduated"]

    if not deployed:
        log.info("Evolution: no deployed strategies to review")
        return {"retired": [], "top_performers": [], "total_deployed": 0}

    from forven.policy import load_pipeline_config
    config = load_pipeline_config()
    retire_cfg = config.get("retirement", {})
    max_fitness = retire_cfg.get("max_fitness", 40)
    max_dd_limit = retire_cfg.get("max_drawdown_pct", 0.15)
    review_window_hours = 7 * 24

    # Score deployed strategies from recent live execution only.
    scored = []
    for s in deployed:
        metrics = _load_recent_execution_metrics(
            s["id"],
            execution_pattern="live%",
            lookback_hours=review_window_hours,
        )
        if not metrics:
            continue
        fitness = score_strategy(metrics)
        scored.append({"strategy": s, "fitness": fitness, "metrics": metrics})

    scored.sort(key=lambda x: x["fitness"])

    # Retire bottom 20% (minimum 1 if fitness < max_fitness)
    n_retire = len(scored) // 5
    if scored and n_retire == 0 and scored[0]["fitness"] < max_fitness:
        n_retire = 1
    retired = []
    for entry in scored[:n_retire]:
        s = entry["strategy"]
        if entry["fitness"] < max_fitness:
            transition = transition_stage(s["id"], "archived", reason="Weekly review: low fitness", actor="system")
            if str(transition.get("to") or "").strip().lower() == "archived":
                retired.append(s["id"])

    # Also retire any with excessive drawdown
    for entry in scored:
        s = entry["strategy"]
        dd = float(entry["metrics"].get("max_drawdown_pct", 0))
        if dd > max_dd_limit and s["id"] not in retired:
            transition = transition_stage(
                s["id"],
                "archived",
                reason="Weekly review: excessive drawdown",
                actor="system",
            )
            if str(transition.get("to") or "").strip().lower() == "archived":
                retired.append(s["id"])

    # Assign post-mortem for retired strategies
    if retired:
        try:
            assign_task(
                agent_id="quant-researcher",
                task_type="post_mortem",
                title=f"Post-Mortem: {', '.join(retired)}",
                description=(
                    f"WEEKLY REVIEW — Post-mortem analysis for retired strategies: {', '.join(retired)}\n\n"
                    "For each retired strategy:\n"
                    "1. Use search_chroma to review its backtest history\n"
                    "2. Identify why it failed (regime change? parameter drift? fundamental flaw?)\n"
                    "3. Extract lessons learned\n"
                    "4. Store post-mortem in ChromaDB via store_chroma (trade_post_mortems collection)\n"
                    "5. Store key insights in narrative memory via store_memory\n\n"
                    "These lessons should inform future ideation cycles."
                ),
                strategy_id=retired[0],
            )
        except Exception as exc:
            log.warning("Could not queue weekly review post-mortem task(s): %s", exc)
        log_activity("info", "evolution", f"Weekly review: retired {', '.join(retired)}, assigned post-mortem")

    # Log top performers
    top_performers = scored[-3:] if len(scored) >= 3 else scored
    top_performers.reverse()
    top_summary = ", ".join(
        f"{e['strategy']['id']}(fitness={e['fitness']:.0f})" for e in top_performers
    )
    log.info("Evolution: top performers: %s", top_summary)
    log_activity("info", "evolution", f"Weekly review: top performers: {top_summary}")

    return {
        "retired": retired,
        "top_performers": [e["strategy"]["id"] for e in top_performers],
        "total_deployed": len(deployed),
    }


def get_evolution_status() -> dict:
    """Get a summary of the evolution pipeline status."""
    strategies = get_strategies()

    by_status = {}
    for s in strategies:
        status = _strategy_stage(s)
        by_status.setdefault(status, []).append(s["id"])

    return {
        "quick_screen": len(by_status.get("quick_screen", [])),
        "gauntlet": len(by_status.get("gauntlet", [])),
        "paper": len(by_status.get("paper", [])),
        "live_graduated": len(by_status.get("live_graduated", [])),
        "archived": len(by_status.get("archived", [])),
        "rejected": len(by_status.get("rejected", [])),
        "total": len(strategies),
        "strategies_by_status": by_status,
    }
