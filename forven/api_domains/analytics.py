import json
import math
import threading
import time

from forven import api_core as core
from forven.db import _now, get_db, get_strategies, table_counts
from forven.exchange.risk import is_trading_allowed
from forven.runtime_health import normalize_daemon_state
from forven.util import normalize_stage, sanitize_json_floats

# Backtest metrics move slowly. A TTL equal to the dashboard's 30s poll rebuilt
# the ~10k-strategy leaderboard on nearly every poll.
_DASHBOARD_LEADERBOARD_TTL_SECONDS = 120.0
# T16-F2: only count recent failures toward dead-letter/health so a single old
# failure cannot pin Health=FAIL forever. Queued/running/succeeded stay lifetime.
_DASHBOARD_FAILED_WINDOW = "-1 day"
_DASHBOARD_CACHE_LOCK = threading.Lock()
_DASHBOARD_REBUILD_LOCK = threading.Lock()
_DASHBOARD_CACHE_ENTRIES: list[dict[str, object]] = []
_DASHBOARD_CACHE_EXPIRES_AT = 0.0


def get_stats() -> dict[str, int]:
    return table_counts()


def get_pipeline_funnel() -> dict[str, object]:
    """Return stage counts and transition flow rates for the last 7 days."""
    with get_db() as conn:
        counts = conn.execute(
            "SELECT stage, COUNT(*) as count FROM strategies GROUP BY stage"
        ).fetchall()

        flows = conn.execute(
            """SELECT from_state, to_state, COUNT(*) as count
               FROM strategy_events
               WHERE datetime(created_at) > datetime('now', '-7 days')
               GROUP BY from_state, to_state"""
        ).fetchall()

    return sanitize_json_floats({
        "counts": {str(row["stage"]): int(row["count"]) for row in counts},
        "flows": [dict(row) for row in flows],
    })


def get_funnel_report(days: int = 7) -> dict[str, object]:
    """P0-1: Daily funnel report — stage counts, gate pass/fail, timeout counts, persistence checks."""
    with get_db() as conn:
        # Stage counts
        stage_counts = {
            str(r["stage"]): int(r["count"])
            for r in conn.execute("SELECT stage, COUNT(*) as count FROM strategies GROUP BY stage").fetchall()
        }

        # Transition flows in window
        flows = [
            dict(r)
            for r in conn.execute(
                """SELECT from_state, to_state, COUNT(*) as count
                   FROM strategy_events
                   WHERE datetime(created_at) > datetime('now', ? || ' days')
                   GROUP BY from_state, to_state""",
                (str(-abs(days)),),
            ).fetchall()
        ]

        # Gate rejection counts by gate and reason_code
        gate_rejections = [
            dict(r)
            for r in conn.execute(
                """SELECT gate, reason_code, COUNT(*) as count
                   FROM gate_rejections
                   WHERE datetime(created_at) > datetime('now', ? || ' days')
                   GROUP BY gate, reason_code
                   ORDER BY count DESC""",
                (str(-abs(days)),),
            ).fetchall()
        ]

        # Timeout counts (tasks that timed out)
        timeout_count = 0
        try:
            timeout_row = conn.execute(
                """SELECT COUNT(*) as count FROM tasks
                   WHERE status IN ('failed', 'timed_out', 'timeout')
                     AND datetime(COALESCE(updated_at, created_at)) > datetime('now', ? || ' days')""",
                (str(-abs(days)),),
            ).fetchone()
            timeout_count = int(timeout_row["count"]) if timeout_row else 0
        except Exception:
            pass

        # Backtest persistence check
        backtest_count = 0
        try:
            bt_row = conn.execute("SELECT COUNT(*) as count FROM backtest_results").fetchone()
            backtest_count = int(bt_row["count"]) if bt_row else 0
        except Exception:
            pass

        total_strategies = sum(stage_counts.values())

    return sanitize_json_floats({
        "period_days": days,
        "stage_counts": stage_counts,
        "total_strategies": total_strategies,
        "flows": flows,
        "gate_rejections": gate_rejections,
        "timeout_count": timeout_count,
        "backtest_results_count": backtest_count,
        "heartbeat_alert": total_strategies > 0 and backtest_count == 0,
    })


def get_code_review_log(days: int = 7, limit: int = 50) -> list[dict[str, object]]:
    """Return code change suggestions logged by agents for operator review."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT message, data, created_at FROM activity_log
               WHERE source = 'code-review-log'
               AND datetime(created_at) > datetime('now', ? || ' days')
               ORDER BY id DESC LIMIT ?""",
            (str(-abs(days)), limit),
        ).fetchall()
    import json as _json
    result = []
    for r in rows:
        entry = {"message": r["message"], "created_at": r["created_at"]}
        try:
            entry["detail"] = _json.loads(r["data"]) if r["data"] else {}
        except Exception:
            entry["detail"] = {}
        result.append(entry)
    return result


def get_model_performance() -> list[dict[str, object]]:
    """Return per-model strategy performance and lifecycle stats."""
    with get_db() as conn:
        stats = conn.execute(
            """SELECT
                 model_id,
                 COUNT(*) as total_created,
                 SUM(CASE WHEN LOWER(COALESCE(stage, status, '')) IN ('live_graduated', 'deployed') THEN 1 ELSE 0 END) as deployed,
                 SUM(CASE WHEN stage = 'archived' THEN 1 ELSE 0 END) as archived,
                 AVG(CASE WHEN metrics IS NOT NULL THEN json_extract(metrics, '$.sharpe') END) as avg_sharpe
               FROM strategies
               WHERE model_id IS NOT NULL
               GROUP BY model_id"""
        ).fetchall()
    return [dict(row) for row in stats]


def list_scanner_scans_stub(limit: int = 200) -> list[dict[str, object]]:
    _ = limit
    return []


def get_scanner_indicator_groups_stub() -> dict[str, object]:
    return {}


def list_tournaments_stub(limit: int = 200) -> list[dict[str, object]]:
    _ = max(1, min(limit, 1000))
    return []


def _quality_tier_from_sharpe(sharpe: float) -> str:
    if sharpe >= 2.0:
        return "elite"
    if sharpe >= 1.0:
        return "strong"
    if sharpe >= 0.0:
        return "marginal"
    return "weak"


def _strategy_metrics(row: dict) -> dict[str, object]:
    parsed = core._safe_json((row or {}).get("metrics"))
    return parsed if isinstance(parsed, dict) else {}


def _metric_float(metrics: dict[str, object], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in metrics and metrics.get(key) is not None:
            value = core._coerce_float(metrics.get(key), default)
            if math.isfinite(value):
                return value
            return float(default) if math.isfinite(float(default)) else 0.0
    return float(default)


def _metric_int(metrics: dict[str, object], *keys: str, default: int = 0) -> int:
    value = _metric_float(metrics, *keys, default=float(default))
    return int(value) if value >= 0 else 0


def _clone_dashboard_entries(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    return [dict(entry) for entry in entries]


def clear_dashboard_leaderboard_cache() -> None:
    global _DASHBOARD_CACHE_ENTRIES, _DASHBOARD_CACHE_EXPIRES_AT
    global _DASHBOARD_EXCEPTIONS_CACHE, _DASHBOARD_EXCEPTIONS_EXPIRES_AT
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE_ENTRIES = []
        _DASHBOARD_CACHE_EXPIRES_AT = 0.0
        _DASHBOARD_EXCEPTIONS_CACHE = None
        _DASHBOARD_EXCEPTIONS_EXPIRES_AT = 0.0


def _compute_dashboard_leaderboard_entries() -> list[dict[str, object]]:
    rows = get_strategies()
    entries: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metrics = _strategy_metrics(row)
        sharpe = _metric_float(metrics, "sharpe_ratio", "sharpe", default=0.0)
        total_return = _metric_float(metrics, "total_return", "total_return_pct", "pnl_pct", default=0.0)
        monthly_return = _metric_float(metrics, "monthly_return_pct", default=0.0)
        annualized_return = _metric_float(metrics, "annualized_return_pct", default=(monthly_return * 12.0))
        win_rate_raw = _metric_float(metrics, "win_rate", "winRate", default=0.0)
        win_rate = win_rate_raw if win_rate_raw > 1.0 else (win_rate_raw * 100.0)
        strategy_id = str(row.get("id") or "").strip()
        symbol = str(row.get("symbol") or "UNKNOWN").strip().upper() or "UNKNOWN"
        timeframe = str(row.get("timeframe") or "--").strip() or "--"
        entries.append(
            {
                "id": strategy_id or f"{row.get('name') or 'strategy'}:{symbol}:{timeframe}",
                "strategy_name": str(row.get("name") or strategy_id or "Unnamed Strategy").strip() or "Unnamed Strategy",
                "display_name": str(row.get("display_name") or "").strip() or None,
                "symbol": symbol,
                "timeframe": timeframe,
                "sharpe_ratio": sharpe,
                "total_return": total_return,
                "monthly_return_pct": monthly_return,
                "annualized_return_pct": annualized_return,
                "max_drawdown": _metric_float(metrics, "max_drawdown", "max_drawdown_pct", default=0.0),
                "win_rate": win_rate,
                "total_trades": _metric_int(metrics, "total_trades", "trades", default=0),
                "profit_factor": _metric_float(metrics, "profit_factor", "pf", default=0.0),
                "sortino_ratio": _metric_float(metrics, "sortino_ratio", default=0.0),
                "calmar_ratio": _metric_float(metrics, "calmar_ratio", default=0.0),
                "source": "core",
                "scan_id": "",
                "lifecycle_strategy_id": strategy_id or None,
                "pinned_backtest_id": str(row.get("pinned_backtest_id") or "").strip(),
                "tier": _quality_tier_from_sharpe(sharpe),
                "mini_equity": [],
                "deflated_sharpe": _metric_float(metrics, "deflated_sharpe", default=sharpe),
                "created_at": str(row.get("updated_at") or row.get("created_at") or _now()),
            }
        )
    return sanitize_json_floats(entries)


def _dashboard_leaderboard_entries() -> list[dict[str, object]]:
    global _DASHBOARD_CACHE_ENTRIES, _DASHBOARD_CACHE_EXPIRES_AT

    now_monotonic = time.monotonic()
    with _DASHBOARD_CACHE_LOCK:
        if _DASHBOARD_CACHE_ENTRIES and now_monotonic < _DASHBOARD_CACHE_EXPIRES_AT:
            return _clone_dashboard_entries(_DASHBOARD_CACHE_ENTRIES)

    # Dashboard panels that miss together wait for one rebuild and reuse it,
    # instead of each loading every strategy at once.
    with _DASHBOARD_REBUILD_LOCK:
        with _DASHBOARD_CACHE_LOCK:
            if _DASHBOARD_CACHE_ENTRIES and now_monotonic < _DASHBOARD_CACHE_EXPIRES_AT:
                return _clone_dashboard_entries(_DASHBOARD_CACHE_ENTRIES)
        entries = _compute_dashboard_leaderboard_entries()
        with _DASHBOARD_CACHE_LOCK:
            _DASHBOARD_CACHE_ENTRIES = _clone_dashboard_entries(entries)
            _DASHBOARD_CACHE_EXPIRES_AT = now_monotonic + _DASHBOARD_LEADERBOARD_TTL_SECONDS
            return _clone_dashboard_entries(_DASHBOARD_CACHE_ENTRIES)


def _stage_status_groups() -> list[tuple[object, object, int]]:
    """Strategy counts per stored ``(stage, status)`` pair.

    The stage KPIs used to load every strategy row (~85MB of params, metrics and
    notes) on each dashboard poll just to count them.
    """
    with get_db() as conn:
        rows = conn.execute(
            "SELECT stage, status, COUNT(*) AS n FROM strategies GROUP BY stage, status"
        ).fetchall()
    return [(row["stage"], row["status"], int(row["n"] or 0)) for row in rows]


def _reported_stage(stage: object, status: object) -> object:
    """The stage a ``get_strategies()`` row reports: normalized stage, else status."""
    return normalize_stage(stage or status) or status


def _best_stored_sharpe() -> float:
    """Highest stored Sharpe across every strategy; -inf when none has one.

    SQLite extracts the two keys from well-formed metrics. Only rows it rejects
    (Python writes NaN/Infinity tokens) are parsed here, instead of decoding every
    strategy's metrics JSON under the GIL on each poll.
    """
    best = float("-inf")
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT CASE WHEN json_valid(metrics) THEN NULL ELSE metrics END AS raw,
                   CASE WHEN json_valid(metrics) THEN json_extract(metrics, '$.sharpe_ratio') END AS sharpe_ratio,
                   CASE WHEN json_valid(metrics) THEN json_extract(metrics, '$.sharpe') END AS sharpe
            FROM strategies
            WHERE metrics IS NOT NULL
            """
        ).fetchall()
    for row in rows:
        if row["raw"] is not None:
            metrics = _strategy_metrics({"metrics": row["raw"]})
        else:
            metrics = {key: row[key] for key in ("sharpe_ratio", "sharpe") if row[key] is not None}
        best = max(best, _metric_float(metrics, "sharpe_ratio", "sharpe", default=float("-inf")))
    return best


def _get_task_queue_counts() -> dict[str, int]:
    """Count tasks by status from the tasks table for real queue metrics."""
    counts = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0}
    failed_statuses = ("failed", "error", "errored", "failed_permanent")
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
            ).fetchall()
            for row in rows:
                raw = str(row["status"] or "").strip().lower()
                if raw in ("pending", "queued"):
                    counts["queued"] += int(row["cnt"])
                elif raw in ("running", "claimed", "processing"):
                    counts["running"] += int(row["cnt"])
                elif raw in ("succeeded", "done", "completed", "complete", "success"):
                    counts["succeeded"] += int(row["cnt"])
            # T16-F2: time-bound ONLY the failed bucket (feeds dead_letter_jobs/
            # health_ok) so one old failure cannot pin Health=FAIL forever.
            failed_row = conn.execute(
                "SELECT COUNT(*) as cnt FROM tasks "
                "WHERE LOWER(TRIM(status)) IN (?, ?, ?, ?) "
                "AND datetime(COALESCE(completed_at, created_at)) > datetime('now', ?) "
                "AND dismissed_at IS NULL",
                (*failed_statuses, _DASHBOARD_FAILED_WINDOW),
            ).fetchone()
            counts["failed"] = int(failed_row["cnt"]) if failed_row else 0
    except Exception:
        pass
    return counts


def _get_autopilot_settings() -> dict[str, object]:
    """Read autopilot-related settings from the pipeline settings KV store."""
    from forven.db import kv_get as _kv_get
    defaults: dict[str, object] = {
        "autopilot_enabled": True,
        "autopilot_worker_concurrency": 4,
    }
    try:
        raw = _kv_get("forven:pipeline:settings", {})
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        defaults.update(raw)
    return defaults


def get_dashboard_overview_stub() -> dict[str, object]:
    daemon = normalize_daemon_state(write_back=True)
    trading_allowed, trading_reason = is_trading_allowed()

    lifecycle_counts: dict[str, int] = {}
    # T16-F1: Pipeline KPI must exclude terminal strategies (the inflated "213"
    # class). Filter on canonical normalize_stage BEFORE _to_lifecycle_state.
    _terminal_stages = {"archived", "rejected", "backtest_failed"}
    pipeline_count = 0
    strategy_count = 0
    for stage, status, count in _stage_status_groups():
        reported = _reported_stage(stage, status)
        state = core._to_lifecycle_state(reported)
        lifecycle_counts[state] = lifecycle_counts.get(state, 0) + count
        if normalize_stage(reported) not in _terminal_stages:
            pipeline_count += count
        strategy_count += count
    best_sharpe = _best_stored_sharpe()

    daemon_running = bool(daemon.get("running"))

    # Real queue metrics from tasks table
    queue_counts = _get_task_queue_counts()
    active_workers = queue_counts["running"]

    # Read configured concurrency from settings
    ap_settings = _get_autopilot_settings()
    worker_concurrency = int(ap_settings.get("autopilot_worker_concurrency") or 4)
    autopilot_enabled = bool(ap_settings.get("autopilot_enabled", True))

    # Dead letters = permanently failed tasks
    dead_letter_jobs = queue_counts["failed"]

    # Compute last tick error from daemon state
    last_tick_error = daemon.get("last_tick_error") or None
    if daemon.get("stale_process_detected"):
        last_tick_error = last_tick_error or "Stale daemon process detected — automatic recovery applied."

    return sanitize_json_floats({
        "kpis": {
            "total_tested": strategy_count,
            "best_sharpe": best_sharpe if best_sharpe != float("-inf") else 0.0,
            "active_scans": int(daemon.get("scan_count") or 0),
            "signals_today": 0,
            "pipeline_count": pipeline_count,
            "data_coverage": 0,
        },
        "lifecycle_counts": lifecycle_counts,
        "blocked_count": 0,
        "last_ingestion_at": daemon.get("last_scan"),
        "autopilot": {
            "initialized": True,
            "running": daemon_running and autopilot_enabled,
            "paused": not bool(trading_allowed) or not autopilot_enabled,
            "run_id": str(daemon.get("run_id") or "") or None,
            "worker_concurrency": worker_concurrency,
            "active_workers": active_workers,
            "queued_jobs": queue_counts["queued"],
            "dead_letter_jobs": dead_letter_jobs,
            "last_tick_error": str(last_tick_error) if last_tick_error else None,
            "health_ok": daemon_running and dead_letter_jobs == 0,
            "disabled_reason": (
                "Autopilot disabled in settings" if not autopilot_enabled
                else str(trading_reason) if (not trading_allowed and trading_reason)
                else None
            ),
        },
        "timestamp": _now(),
    })


def get_dashboard_kpis_stub() -> dict[str, object]:
    overview = get_dashboard_overview_stub()
    return overview.get("kpis", {})


def get_dashboard_activity_stub(limit: int = 50) -> list[dict[str, object]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, source, message, data, created_at FROM activity_log ORDER BY id DESC LIMIT ?",
            (max(1, int(limit or 50)),),
        ).fetchall()

    payload: list[dict[str, object]] = []
    for row in rows:
        source = str(row["source"] or "").strip().lower()
        message = str(row["message"] or "").strip() or "Activity"
        kind = "transition" if ("strategy" in source or "pipeline" in source or "transition" in message.lower()) else "task"
        detail_data = core._safe_json(row["data"])
        if isinstance(detail_data, (dict, list)):
            details = json.dumps(detail_data, separators=(",", ":"))
        else:
            details = str(row["data"] or "")
        payload.append(
            {
                "type": kind,
                "message": message,
                "details": details,
                "timestamp": row["created_at"],
            }
        )
    return payload


def get_dashboard_actions_stub() -> list[dict[str, object]]:
    overview = get_dashboard_overview_stub()
    autopilot = overview.get("autopilot", {}) if isinstance(overview, dict) else {}
    actions: list[dict[str, object]] = []
    if not bool(autopilot.get("running")):
        actions.append(
            {
                "id": "start-daemon",
                "label": "Start Daemon",
                "description": "Daemon appears offline. Restart orchestration services.",
                "href": "/lab?tab=247",
                "priority": 100,
                "kind": "critical",
            }
        )
    if bool(autopilot.get("paused")) or autopilot.get("disabled_reason"):
        actions.append(
            {
                "id": "review-risk",
                "label": "Review Risk",
                "description": str(autopilot.get("disabled_reason") or "Trading is paused. Review risk controls."),
                "href": "/risk",
                "priority": 90,
                "kind": "warning",
            }
        )
    actions.append(
        {
            "id": "open-pipeline",
            "label": "Open Backtest Manager",
            "description": "Review strategy phases and active handoffs in the Backtest File Manager.",
            "href": "/backtest",
            "priority": 70,
            "kind": "info",
        }
    )
    return actions


def get_dashboard_leaderboard_stub(
    sort_by: str = "sharpe_ratio",
    limit: int = 30,
    min_sharpe: float | None = None,
    symbol: str | None = None,
    timeframe: str | None = None,
    tier: str | None = None,
) -> list[dict[str, object]]:
    entries = _dashboard_leaderboard_entries()
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_timeframe = str(timeframe or "").strip()
    normalized_tier = str(tier or "").strip().lower()

    filtered = []
    for entry in entries:
        if normalized_symbol and str(entry.get("symbol") or "").upper() != normalized_symbol:
            continue
        if normalized_timeframe and str(entry.get("timeframe") or "") != normalized_timeframe:
            continue
        if normalized_tier and str(entry.get("tier") or "").lower() != normalized_tier:
            continue
        if min_sharpe is not None and core._coerce_float(entry.get("sharpe_ratio"), 0.0) < float(min_sharpe):
            continue
        filtered.append(entry)

    key = str(sort_by or "sharpe_ratio").strip().lower() or "sharpe_ratio"

    def _value(row: dict[str, object]):
        value = row.get(key)
        if isinstance(value, str):
            return value
        return core._coerce_float(value, 0.0)

    filtered.sort(key=_value, reverse=True)
    return sanitize_json_floats(filtered[: max(1, int(limit or 30))])


def get_dashboard_tier_distribution_stub(scan_id: str | None = None) -> dict[str, object]:
    _ = scan_id
    rows = _dashboard_leaderboard_entries()
    tiers = {"elite": 0, "strong": 0, "marginal": 0, "weak": 0}
    for row in rows:
        tier = str(row.get("tier") or "").strip().lower()
        if tier not in tiers:
            tier = "weak"
        tiers[tier] += 1
    total = tiers["elite"] + tiers["strong"] + tiers["marginal"] + tiers["weak"]
    return {
        "elite": tiers["elite"],
        "strong": tiers["strong"],
        "marginal": tiers["marginal"],
        "weak": tiers["weak"],
        "tiers": tiers,
        "total": total,
    }


def get_dashboard_winners_stub(limit: int = 10) -> list[dict[str, object]]:
    entries = _dashboard_leaderboard_entries()
    entries.sort(
        key=lambda row: (
            core._coerce_float(row.get("deflated_sharpe"), core._coerce_float(row.get("sharpe_ratio"), 0.0)),
            core._coerce_float(row.get("total_return"), 0.0),
        ),
        reverse=True,
    )
    winners: list[dict[str, object]] = []
    for row in entries:
        tier = str(row.get("tier") or "weak")
        if tier == "weak":
            continue
        winners.append(
            {
                "id": row.get("id"),
                "strategy_name": row.get("strategy_name"),
                "symbol": row.get("symbol"),
                "timeframe": row.get("timeframe"),
                "deflated_sharpe": row.get("deflated_sharpe"),
                "total_return": row.get("total_return"),
                "monthly_return_pct": row.get("monthly_return_pct"),
                "annualized_return_pct": row.get("annualized_return_pct"),
                "max_drawdown": row.get("max_drawdown"),
                "total_trades": row.get("total_trades"),
                "tier": tier,
                "created_at": row.get("created_at") or _now(),
                "scan_id": row.get("scan_id") or "",
            }
        )
        if len(winners) >= max(1, int(limit or 10)):
            break
    return winners


def get_dashboard_coverage_stub() -> dict[str, object]:
    coverage: dict[str, dict[str, object]] = {}
    for row in _dashboard_leaderboard_entries():
        symbol = str(row.get("symbol") or "").strip().upper()
        timeframe = str(row.get("timeframe") or "").strip()
        if not symbol or not timeframe:
            continue
        key = f"{symbol}:{timeframe}"
        current = coverage.get(key, {"tested_count": 0, "best_sharpe": None})
        current_count = int(current.get("tested_count") or 0) + 1
        sharpe = core._coerce_float(row.get("sharpe_ratio"), 0.0)
        best = current.get("best_sharpe")
        best_float = core._coerce_float(best, float("-inf")) if best is not None else float("-inf")
        current["tested_count"] = current_count
        current["best_sharpe"] = sharpe if sharpe > best_float else best
        coverage[key] = current
    return {"coverage": coverage}


def get_dashboard_equity_curves_stub(scan_id: str | None = None, n: int = 5) -> list[dict[str, object]]:
    """Top-N strategies' stored equity curves for the dashboard Equity Overlay.

    Sourced from each strategy's pinned backtest artifact
    (``strategies.pinned_backtest_id`` -> persisted ``*_equity.json`` loaded via
    ``api_core._load_result_artifacts``). Strategies without a pinned result are
    skipped, so the panel degrades to its existing empty state rather than
    erroring. Never raises — any failure yields an empty list.
    """
    _ = scan_id
    try:
        limit = max(1, min(int(n or 5), 10))
    except Exception:
        limit = 5
    out: list[dict[str, object]] = []
    try:
        entries = sorted(
            _dashboard_leaderboard_entries(),
            key=lambda r: core._coerce_float(r.get("sharpe_ratio"), 0.0),
            reverse=True,
        )
        for entry in entries:
            if len(out) >= limit:
                break
            try:
                # pinned_backtest_id is already carried on the leaderboard entry, so
                # no per-strategy DB query is needed. Load only the equity artifact
                # (not trades/benchmark) via the single-suffix loader.
                pinned = str(entry.get("pinned_backtest_id") or "").strip()
                if not pinned:
                    continue
                raw_equity, _ = core._load_result_json_artifact(pinned, {}, "backtest", "equity")
                curve = core._normalize_equity_points(raw_equity) if raw_equity else None
                if not curve:
                    continue
                out.append({
                    "strategy_name": entry.get("strategy_name"),
                    "sharpe_ratio": entry.get("sharpe_ratio"),
                    "equity_curve": curve,
                })
            except Exception:
                continue
    except Exception:
        return []
    return sanitize_json_floats(out)


def get_strategy_performance() -> list[dict[str, object]]:
    """Aggregate trade stats grouped by strategy."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT strategy,
                   COUNT(*) as total_trades,
                   SUM(CASE WHEN pnl_pct > 0 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN pnl_pct <= 0 THEN 1 ELSE 0 END) as losses,
                   AVG(pnl_pct) as avg_pnl,
                   SUM(pnl_usd) as total_pnl_usd,
                   MAX(pnl_pct) as best_trade,
                   MIN(pnl_pct) as worst_trade,
                   COUNT(CASE WHEN status = 'OPEN' THEN 1 END) as open_count
            FROM trades
            WHERE COALESCE(source, '') NOT LIKE 'bot:%'
            GROUP BY strategy
            """
        ).fetchall()
        return [dict(row) for row in rows]


def dashboard_funnel_stub() -> list[dict[str, object]]:
    counts: dict[str, int] = {}
    for stage, status, count in _stage_status_groups():
        state = core._to_lifecycle_state(_reported_stage(stage, status))
        counts[state] = counts.get(state, 0) + count
    return [{"state": state, "count": count} for state, count in counts.items()]


_DASHBOARD_EXCEPTIONS_CACHE: list[dict[str, object]] | None = None
_DASHBOARD_EXCEPTIONS_EXPIRES_AT = 0.0


def _compute_dashboard_exceptions() -> list[dict[str, object]]:
    """T16-F3: Surface stalled/lost/deadlocked signals by reusing the funnel
    report (gate_rejections / timeout_count / heartbeat_alert)."""
    report = get_funnel_report()
    items: list[dict[str, object]] = []

    if report.get("heartbeat_alert"):
        items.append({
            "kind": "heartbeat",
            "severity": "critical",
            "message": (
                "Strategies exist but no backtest results persisted — "
                "pipeline may be stalled."
            ),
            "count": int(report.get("total_strategies") or 0),
        })

    timeout_count = int(report.get("timeout_count") or 0)
    if timeout_count > 0:
        items.append({
            "kind": "timeout",
            "severity": "warning",
            "message": f"{timeout_count} task(s) timed out or failed in the last {int(report.get('period_days') or 7)} days.",
            "count": timeout_count,
        })

    for rej in report.get("gate_rejections") or []:
        if not isinstance(rej, dict):
            continue
        items.append({
            "kind": "gate_rejection",
            "severity": "info",
            "message": (
                f"Gate '{rej.get('gate')}' rejected "
                f"{int(rej.get('count') or 0)} strategy(ies): {rej.get('reason_code')}"
            ),
            "gate": rej.get("gate"),
            "reason_code": rej.get("reason_code"),
            "count": int(rej.get("count") or 0),
        })

    return items


def dashboard_exceptions_stub(limit: int = 30) -> list[dict[str, object]]:
    global _DASHBOARD_EXCEPTIONS_CACHE, _DASHBOARD_EXCEPTIONS_EXPIRES_AT
    try:
        now_monotonic = time.monotonic()
        with _DASHBOARD_CACHE_LOCK:
            cached = _DASHBOARD_EXCEPTIONS_CACHE
            fresh = cached is not None and now_monotonic < _DASHBOARD_EXCEPTIONS_EXPIRES_AT
        if not fresh:
            computed = _compute_dashboard_exceptions()
            with _DASHBOARD_CACHE_LOCK:
                _DASHBOARD_EXCEPTIONS_CACHE = computed
                _DASHBOARD_EXCEPTIONS_EXPIRES_AT = (
                    now_monotonic + _DASHBOARD_LEADERBOARD_TTL_SECONDS
                )
            cached = computed
        items = list(cached or [])
        return sanitize_json_floats(items[: max(0, int(limit))])
    except Exception:
        return []


def dashboard_suggestions_stub() -> list[dict[str, object]]:
    return []


def get_research_feed_metrics_stub() -> dict[str, int]:
    return {
        "total": 0,
        "new_count": 0,
        "reviewed_count": 0,
        "ignored_count": 0,
        "reviewed_this_week": 0,
        "total_count": 0,
    }


__all__ = [
    "clear_dashboard_leaderboard_cache",
    "dashboard_exceptions_stub",
    "dashboard_funnel_stub",
    "dashboard_suggestions_stub",
    "get_dashboard_actions_stub",
    "get_dashboard_activity_stub",
    "get_dashboard_coverage_stub",
    "get_dashboard_equity_curves_stub",
    "get_dashboard_kpis_stub",
    "get_dashboard_leaderboard_stub",
    "get_dashboard_overview_stub",
    "get_dashboard_tier_distribution_stub",
    "get_dashboard_winners_stub",
    "get_funnel_report",
    "get_model_performance",
    "get_pipeline_funnel",
    "get_research_feed_metrics_stub",
    "get_scanner_indicator_groups_stub",
    "get_stats",
    "get_strategy_performance",
    "list_scanner_scans_stub",
    "list_tournaments_stub",
]
