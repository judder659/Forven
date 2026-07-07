"""Tools for the unified in-app assistant (page-aware operator chat).

These are READ-grounding tools (so the assistant can answer about portfolio,
pipeline, regime, and any strategy without burning tool round-trips guessing)
plus operator-grade ACTION tools the operator explicitly authorized for
direct (no-confirmation) use:

  * ``assistant_create_strategy`` — creates a strategy from a natural-language
    idea. This closes the long-standing gap where ``create_strategy`` was
    unusable from chat because it demanded a ``hypothesis_id`` the chat had no
    way to mint. Here we mint a lightweight *operator* hypothesis first
    (``source_type='operator_manual'``, honestly attributed) and then register
    the strategy against it.
  * ``assistant_run_backtest`` — runs a LOCAL backtest for a strategy (same
    engine the deepdive tools use), avoiding the remote HTTP backtest service.
  * ``assistant_register_strategy_file`` — register a custom strategy .py file
    (drop zone) into quick_screen.
  * ``assistant_enqueue_candidate`` — pre-screen a strategy and submit it into
    the GAUNTLET (automated evaluation). Enters evaluation only; never paper/live.

Mutating lifecycle actions that put a strategy live or spawn work (promote to
paper/live, assign work) are intentionally NOT here — they live in the confirm
tier and route through the operator confirm card.
"""

import json as _json

from forven.agents.context import _current_agent_id_var  # noqa: F401 (kept for parity/future use)
from forven.agents.tool_registry import register_tool
from forven.db import get_db

# Common, certifiable strategy families surfaced to the model so it picks a real
# runtime type instead of inventing one (which create_strategy rejects).
_COMMON_FAMILIES = (
    "rsi_momentum, macd, stochastic, williams_r, donchian, bollinger, ema_cross, "
    "atr_breakout, adx_dmi, vwap, mean_reversion, supertrend"
)


# ---------------------------------------------------------------------------
# Read / grounding tools
# ---------------------------------------------------------------------------

@register_tool(
    name="get_portfolio_status",
    description=(
        "Read the live portfolio snapshot: account equity, high-water mark, "
        "drawdown, daily PnL, market regime, kill-switch state, and open positions. "
        "Use this to answer 'how are we doing?' / risk questions."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    # risk-manager: read-only introspection for the scheduled risk audit.
    permissions={"brain", "role:risk-manager", None},
)
def _tool_get_portfolio_status() -> str:
    from forven.db import get_open_trades
    from forven.portfolio_status import portfolio_status_snapshot

    # Reads the daemon's REAL risk-tick snapshot (KV daemon_state) — the same
    # numbers the kill-switch and daily-loss guard act on. The previous source
    # (KV "status", camelCase fields) was never written by anything, so this
    # tool reported equity=0/HWM=0 forever and risk audits ran blind.
    snapshot = portfolio_status_snapshot()
    open_trades = get_open_trades() or []
    out = {
        "kill_switch_active": snapshot["kill_switch_active"],
        "daily_halt": snapshot["daily_halt"],
        "account_equity": snapshot["account_equity"],
        "equity_available": snapshot["equity_available"],
        "equity_source": snapshot["equity_source"],
        "high_water_mark": snapshot["high_water_mark"],
        "drawdown_pct": snapshot["drawdown_pct"],
        "daily_pnl_pct": snapshot["daily_pnl_pct"],
        "regime": snapshot["regime"],
        "fear_greed": snapshot["fear_greed"],
        "synced_at": snapshot["synced_at"],
        "daemon_running": snapshot["daemon_running"],
        "open_position_count": len(open_trades),
        "open_positions": [
            {
                "asset": t.get("asset"),
                "direction": t.get("direction"),
                "entry_price": t.get("entry_price"),
                "strategy": t.get("strategy"),
                "pnl_pct": t.get("pnl_pct"),
            }
            for t in open_trades[:25]
        ],
    }
    return _json.dumps(out, indent=2, default=str)


@register_tool(
    name="get_pipeline_status",
    description=(
        "Read the strategy evolution pipeline: counts and example names by stage "
        "(quick_screen, gauntlet, paper, live_graduated, archived, rejected). "
        "Use this to answer 'what's in the pipeline?'."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    permissions={"brain", None},
)
def _tool_get_pipeline_status() -> str:
    from forven.context import _format_evolution_status

    text = _format_evolution_status()
    return text or "No strategies in the pipeline yet."


@register_tool(
    name="get_market_regime",
    description="Read the current market regime summary for tracked assets.",
    input_schema={"type": "object", "properties": {}, "required": []},
    permissions={"brain", None},
)
def _tool_get_market_regime() -> str:
    try:
        from forven.regime import format_regime_summary

        text = format_regime_summary()
        return text or "Regime data is not available right now."
    except Exception as exc:  # pragma: no cover - defensive
        return f"Could not read market regime: {exc}"


@register_tool(
    name="get_strategy_detail",
    description=(
        "Read full detail for one strategy by id: name, type, symbol, timeframe, "
        "stage, default params, and headline metrics (fitness, sharpe, profit factor, "
        "max drawdown). Use this when the user asks about a specific strategy or 'this strategy'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy id, e.g. S00719"},
        },
        "required": ["strategy_id"],
    },
    # risk-manager: read-only introspection for the scheduled risk audit.
    permissions={"brain", "role:risk-manager", None},
)
def _tool_get_strategy_detail(strategy_id: str) -> str:
    sid = str(strategy_id or "").strip()
    if not sid:
        return "Error: strategy_id is required."
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, type, runtime_type, symbol, timeframe, params, stage, "
            "status, metrics, hypothesis_id, notes FROM strategies WHERE id = ?",
            (sid,),
        ).fetchone()
    if not row:
        return f"No strategy found with id {sid}."
    d = dict(row)
    for key in ("params", "metrics"):
        raw = d.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                d[key] = _json.loads(raw)
            except Exception:
                pass
    metrics = d.get("metrics") if isinstance(d.get("metrics"), dict) else {}
    out = {
        "id": d.get("id"),
        "name": d.get("name"),
        "type": d.get("runtime_type") or d.get("type"),
        "symbol": d.get("symbol"),
        "timeframe": d.get("timeframe"),
        "stage": d.get("stage") or d.get("status"),
        "params": d.get("params"),
        "hypothesis_id": d.get("hypothesis_id"),
        "metrics": {
            "fitness_score": metrics.get("fitness_score"),
            "sharpe": metrics.get("sharpe_ratio", metrics.get("sharpe")),
            "profit_factor": metrics.get("profit_factor"),
            "win_rate": metrics.get("win_rate"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "total_trades": metrics.get("total_trades"),
        },
        "notes": (d.get("notes") or "")[:500],
    }
    return _json.dumps(out, indent=2, default=str)


# ---------------------------------------------------------------------------
# App guide + navigation — the assistant as the operator's guide to the app.
# ---------------------------------------------------------------------------

@register_tool(
    name="get_app_guide",
    description=(
        "Look up the built-in Forven app guide. With no topic: the full index "
        "(every page, all how-to walkthroughs, all concepts). With a topic: the "
        "matching entry — a how-to slug (e.g. 'promote-to-paper', 'go-live', "
        "'add-data', 'why-blocked'), a concept (e.g. 'gauntlet', 'gates', "
        "'kill-switch'), a page route ('/lab'), or a page name ('The Forge'). "
        "Use this whenever the user asks how to do something in the app or what "
        "a page/feature is for."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "How-to slug, concept slug, page route, page name, or free text. Omit for the index.",
            },
        },
        "required": [],
    },
    permissions={"brain", None},
)
def _tool_get_app_guide(topic: str = "") -> str:
    from forven.assistant_guide import lookup_guide, render_guide_index

    q = str(topic or "").strip()
    return lookup_guide(q) if q else render_guide_index()


@register_tool(
    name="open_app_page",
    description=(
        "Navigate the user's browser to a page inside the Forven app (e.g. '/lab', "
        "'/risk', '/lab/strategy/S00719', '/settings'). Only known in-app routes are "
        "allowed. Use when the user asks to go somewhere, or when a walkthrough "
        "starts on another page and they've agreed to head there. Tell the user "
        "where you took them."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "route": {"type": "string", "description": "In-app route path starting with '/'."},
        },
        "required": ["route"],
    },
    permissions={"brain", None},
)
def _tool_open_app_page(route: str) -> str:
    from forven.assistant_guide import is_valid_app_route

    r = str(route or "").strip()
    if not is_valid_app_route(r):
        return (
            f"Error: '{r}' is not a known app route. Use get_app_guide for the page map; "
            "routes must be in-app paths like /lab or /lab/strategy/S00719."
        )
    # The actual navigation is performed client-side: the session loop emits a
    # 'navigate' SSE event when this tool returns ok.
    return _json.dumps({"ok": True, "route": r, "message": f"Opening {r} for the user."})


# ---------------------------------------------------------------------------
# Read / grounding tools — broad app state (all read-only)
# ---------------------------------------------------------------------------

@register_tool(
    name="list_strategies",
    description=(
        "List strategies with headline metrics, optionally filtered by stage "
        "(quick_screen, gauntlet, paper, live_graduated, archived, rejected) and/or "
        "a search string (matched against id/name/symbol). Use this to answer "
        "'what's on paper?', 'find my BTC strategies', etc. For one strategy's full "
        "detail use get_strategy_detail; for why it can/can't advance use get_gate_report."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "stage": {"type": "string", "description": "Optional stage filter."},
            "search": {"type": "string", "description": "Optional substring match on id/name/symbol."},
            "limit": {"type": "integer", "description": "Max rows (default 25, max 100)."},
        },
        "required": [],
    },
    permissions={"brain", None},
)
def _tool_list_strategies(stage: str = "", search: str = "", limit: int = 25) -> str:
    from forven.strategy_lifecycle import read_strategies

    try:
        cap = max(1, min(int(limit or 25), 100))
    except (TypeError, ValueError):
        cap = 25
    q = str(search or "").strip().lower()
    rows = read_strategies(status=(str(stage or "").strip() or None), limit=200 if q else cap)
    out = []
    for r in rows:
        if q:
            hay = " ".join(
                str(r.get(k) or "") for k in ("id", "display_id", "name", "display_name", "symbol")
            ).lower()
            if q not in hay:
                continue
        metrics = r.get("metrics") if isinstance(r.get("metrics"), dict) else {}
        out.append({
            "id": r.get("id"),
            "name": r.get("display_name") or r.get("name"),
            "stage": r.get("stage") or r.get("status"),
            "type": r.get("runtime_type") or r.get("type"),
            "symbol": r.get("symbol"),
            "timeframe": r.get("timeframe"),
            "sharpe": metrics.get("sharpe_ratio", metrics.get("sharpe")),
            "profit_factor": metrics.get("profit_factor"),
            "win_rate": metrics.get("win_rate"),
            "max_drawdown_pct": metrics.get("max_drawdown_pct"),
            "total_trades": metrics.get("total_trades"),
            "updated_at": r.get("updated_at"),
        })
        if len(out) >= cap:
            break
    return _json.dumps({"count": len(out), "strategies": out}, indent=2, default=str)


@register_tool(
    name="get_gate_report",
    description=(
        "Why can/can't a strategy advance: promotion-readiness steps (pass/fail with "
        "detail), gauntlet test rollup (per-test verdicts, robustness score), and — "
        "for paper strategies — paper→live readiness. THE tool for 'why is this "
        "stuck?' / 'what does it still need?'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy id, e.g. S00719"},
        },
        "required": ["strategy_id"],
    },
    permissions={"brain", None},
)
def _tool_get_gate_report(strategy_id: str) -> str:
    sid = str(strategy_id or "").strip()
    if not sid:
        return "Error: strategy_id is required."
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, stage, status FROM strategies WHERE id = ?", (sid,)
        ).fetchone()
    if not row:
        return f"No strategy found with id {sid}."
    stage = row["stage"] or row["status"]
    out: dict = {"strategy_id": sid, "name": row["name"], "stage": stage}

    from forven.policy import check_promotion_readiness

    try:
        readiness = check_promotion_readiness(sid)
        out["promotion_ready"] = readiness.get("ready")
        out["readiness_steps"] = [
            {"name": s.get("name"), "status": s.get("status"), "detail": s.get("detail")}
            for s in (readiness.get("steps") or [])
        ]
    except Exception as exc:
        out["readiness_error"] = str(exc)

    try:
        from forven.gauntlet.status import get_strategy_gauntlet_status

        g = get_strategy_gauntlet_status(sid)
        if isinstance(g, dict) and not g.get("error"):
            tests = g.get("tests") or {}
            out["gauntlet"] = {
                "passed_tests": g.get("passed_tests"),
                "missing_required": g.get("missing_required"),
                "current_step": g.get("current_step"),
                "robustness_score": g.get("robustness_score", g.get("score")),
                "min_robustness": g.get("min_robustness"),
                "tests": {
                    k: {"status": v.get("status"), "verdict": v.get("verdict"), "stale": v.get("stale")}
                    for k, v in tests.items()
                    if isinstance(v, dict)
                },
            }
    except Exception as exc:
        out["gauntlet_error"] = str(exc)

    if str(stage or "").lower() == "paper":
        from forven.policy import check_paper_live_readiness

        try:
            live = check_paper_live_readiness(sid)
            out["paper_to_live"] = {
                "ready": live.get("ready"),
                "steps": [
                    {"name": s.get("name"), "status": s.get("status"), "detail": s.get("detail")}
                    for s in (live.get("steps") or [])
                ],
            }
        except Exception as exc:
            out["paper_to_live_error"] = str(exc)

    failed = [s["name"] for s in out.get("readiness_steps", []) if s.get("status") == "failed"]
    out["failed_gates"] = failed
    return _json.dumps(out, indent=2, default=str)


@register_tool(
    name="get_recent_trades",
    description=(
        "Read recent trades and/or open positions from the ledger, optionally "
        "filtered by strategy id and status (OPEN or CLOSED). Includes paper, live, "
        "and bot trades with entry/exit prices and PnL."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Optional: OPEN or CLOSED."},
            "strategy_id": {"type": "string", "description": "Optional strategy id filter."},
            "limit": {"type": "integer", "description": "Max rows (default 20, max 50)."},
        },
        "required": [],
    },
    # risk-manager: read-only introspection for the scheduled risk audit.
    permissions={"brain", "role:risk-manager", None},
)
def _tool_get_recent_trades(status: str = "", strategy_id: str = "", limit: int = 20) -> str:
    from forven.db import get_open_trades, get_recent_trades

    try:
        cap = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        cap = 20
    want_status = str(status or "").strip().upper()
    sid = str(strategy_id or "").strip()

    if want_status == "OPEN":
        rows = get_open_trades() or []
    else:
        rows = get_recent_trades(limit=200) or []
        if want_status:
            rows = [r for r in rows if str(r.get("status") or "").upper() == want_status]
    if sid:
        rows = [
            r for r in rows
            if sid in (str(r.get("strategy_id") or ""), str(r.get("strategy") or ""))
        ]

    out = [
        {
            "id": r.get("display_id") or r.get("id"),
            "strategy": r.get("strategy_name") or r.get("strategy_id") or r.get("strategy"),
            "asset": r.get("asset") or r.get("symbol"),
            "direction": r.get("direction"),
            "status": r.get("status"),
            "execution_type": r.get("execution_type"),
            "entry_price": r.get("entry_price"),
            "exit_price": r.get("exit_price"),
            "pnl_pct": r.get("pnl_pct"),
            "pnl_usd": r.get("pnl_usd"),
            "opened_at": r.get("opened_at"),
            "closed_at": r.get("closed_at"),
        }
        for r in rows[:cap]
    ]
    return _json.dumps({"count": len(out), "trades": out}, indent=2, default=str)


@register_tool(
    name="get_settings_overview",
    description=(
        "Read the current app settings (risk/pipeline/gate thresholds, execution "
        "mode, regime gating, data/lab/notification config). Secrets are never "
        "included. Use to answer 'what is X set to?' before pointing the user at "
        "the Settings page to change it."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    permissions={"brain", None},
)
def _tool_get_settings_overview() -> str:
    from forven.api_core import get_settings

    sensitive = ("key", "token", "secret", "webhook", "password", "private", "wallet", "address")
    settings = get_settings() or {}
    out = {}
    for k, v in settings.items():
        kl = str(k).lower()
        # Booleans like hyperliquid_has_key / *_configured are safe and useful.
        if any(s in kl for s in sensitive) and not isinstance(v, bool):
            continue
        out[k] = v
    blob = _json.dumps(out, indent=2, default=str)
    if len(blob) > 20000:
        blob = blob[:20000] + "\n… (truncated)"
    return blob


@register_tool(
    name="get_ops_overview",
    description=(
        "One-shot operational snapshot: system mode, generation pause state, "
        "kill-switch, pending approvals, scheduler jobs, recent health alerts, and "
        "notification stats. Use for 'is everything OK?', 'anything waiting on me?', "
        "and diagnosing why nothing seems to be happening."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    permissions={"brain", None},
)
def _tool_get_ops_overview() -> str:
    out: dict = {}
    try:
        from forven.control_plane.ops import get_system_mode_status

        mode = get_system_mode_status() or {}
        out["system"] = {
            "mode": mode.get("system_mode"),
            "paused": mode.get("paused"),
            "generation_paused": mode.get("generation_paused"),
        }
    except Exception as exc:
        out["system_error"] = str(exc)

    try:
        from forven.portfolio_status import portfolio_status_snapshot

        snapshot = portfolio_status_snapshot()
        out["risk"] = {
            "kill_switch_active": snapshot["kill_switch_active"],
            "daily_halt": snapshot["daily_halt"],
            "account_equity": snapshot["account_equity"],
            "equity_available": snapshot["equity_available"],
            "daily_pnl_pct": snapshot["daily_pnl_pct"],
            "drawdown_pct": snapshot["drawdown_pct"],
        }
    except Exception as exc:
        out["risk_error"] = str(exc)

    try:
        from forven.control_plane.approvals import get_approvals_list

        pending = get_approvals_list(status="pending_approval", limit=10) or []
        out["approvals"] = {
            "pending_count": len(pending),
            "pending": [
                {
                    "id": a.get("id"),
                    "type": a.get("approval_type"),
                    "target": a.get("target_id"),
                    "created_at": a.get("created_at"),
                }
                for a in pending[:10]
            ],
        }
    except Exception as exc:
        out["approvals_error"] = str(exc)

    try:
        from forven.scheduler import get_jobs

        jobs = get_jobs() or []
        out["scheduler"] = [
            {
                "name": j.get("name"),
                "enabled": j.get("enabled"),
                "schedule": j.get("schedule_expr"),
                "next_run_at": j.get("next_run_at"),
            }
            for j in jobs[:30]
        ]
    except Exception as exc:
        out["scheduler_error"] = str(exc)

    try:
        from forven.health_monitor import get_health_monitor

        monitor = get_health_monitor()
        if monitor is not None:
            alerts = monitor.state.get_alerts(limit=5) or []
            out["recent_alerts"] = [a.to_dict() for a in alerts]
        else:
            out["recent_alerts"] = []
    except Exception as exc:
        out["alerts_error"] = str(exc)

    try:
        from forven.control_plane.notifications import get_notifications_list

        notif = get_notifications_list(limit=5, actionable=True) or {}
        out["notifications"] = {
            "stats": notif.get("stats"),
            "actionable": [
                {"title": n.get("title"), "severity": n.get("severity"), "created_at": n.get("created_at")}
                for n in (notif.get("items") or [])[:5]
            ],
        }
    except Exception as exc:
        out["notifications_error"] = str(exc)

    return _json.dumps(out, indent=2, default=str)


@register_tool(
    name="list_hypotheses",
    description=(
        "List crucibles (trading-idea hypotheses) with status and strategy counts, "
        "optionally filtered by status or a search string. Use for 'any promising "
        "ideas?', 'what ideas are under test?'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Optional status filter."},
            "search": {"type": "string", "description": "Optional search string."},
            "limit": {"type": "integer", "description": "Max rows (default 20, max 50)."},
        },
        "required": [],
    },
    permissions={"brain", None},
)
def _tool_list_hypotheses(status: str = "", search: str = "", limit: int = 20) -> str:
    from forven.api_domains.hypotheses import list_hypotheses_page

    try:
        cap = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        cap = 20
    page = list_hypotheses_page(
        status=(str(status or "").strip() or None),
        search=(str(search or "").strip() or None),
        limit=cap,
        offset=0,
    )
    rows = page.get("hypotheses") or []
    out = [
        {
            "id": h.get("id"),
            "display_id": h.get("display_id"),
            "title": h.get("title"),
            "status": h.get("status"),
            "lane": h.get("lane"),
            "source_type": h.get("source_type"),
            "strategy_count": h.get("strategy_count"),
        }
        for h in rows[:cap]
    ]
    return _json.dumps({"total": page.get("total"), "hypotheses": out}, indent=2, default=str)


@register_tool(
    name="list_bots",
    description=(
        "List Bot Factory bots: name, model, execution mode (paper/live), runtime "
        "status, open positions, closed trades, realized PnL."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    # risk-manager: read-only introspection for the scheduled risk audit.
    permissions={"brain", "role:risk-manager", None},
)
def _tool_list_bots() -> str:
    from forven.api_domains.bot_factory import api_list_bots

    bots = api_list_bots() or []
    out = [
        {
            "id": b.get("id"),
            "name": b.get("name"),
            "model": b.get("model"),
            "execution_mode": b.get("execution_mode"),
            "runtime_status": b.get("runtime_status") or b.get("status"),
            "open_positions": b.get("open_positions"),
            "closed_trades": b.get("closed_trades"),
            "realized_pnl": b.get("realized_pnl"),
            "live_wallet": b.get("live_wallet"),
        }
        for b in bots[:50]
    ]
    return _json.dumps({"count": len(out), "bots": out}, indent=2, default=str)


@register_tool(
    name="list_routines",
    description=(
        "List scheduled routines (recurring agent jobs that post to Discord): "
        "name, schedule, delivery channel, enabled state, and the job prompt."
    ),
    input_schema={"type": "object", "properties": {}, "required": []},
    permissions={"brain", None},
)
def _tool_list_routines() -> str:
    from forven.control_plane.routines import list_routines

    routines = list_routines() or []
    out = [
        {
            "id": r.get("id"),
            "name": r.get("name"),
            "schedule": r.get("cron_expr"),
            "channel": r.get("channel"),
            "enabled": r.get("enabled"),
            "prompt": str(r.get("prompt") or "")[:160],
        }
        for r in routines[:50]
    ]
    return _json.dumps({"count": len(out), "routines": out}, indent=2, default=str)


@register_tool(
    name="list_agent_tasks",
    description=(
        "List recent agent tasks (research, strategy development, repairs, ...): "
        "agent, title, status, cost. Optional status filter (e.g. pending, running, "
        "completed, failed). Use for 'what are the agents doing?'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Optional status filter."},
            "limit": {"type": "integer", "description": "Max rows (default 20, max 50)."},
        },
        "required": [],
    },
    permissions={"brain", None},
)
def _tool_list_agent_tasks(status: str = "", limit: int = 20) -> str:
    from forven.api_domains.tasks import get_agent_tasks

    try:
        cap = max(1, min(int(limit or 20), 50))
    except (TypeError, ValueError):
        cap = 20
    want = str(status or "").strip().lower()
    rows = get_agent_tasks() or []
    if want:
        rows = [r for r in rows if str(r.get("status") or "").lower() == want]
    out = [
        {
            "id": t.get("display_id") or t.get("id"),
            "agent": t.get("agent_id"),
            "type": t.get("type"),
            "title": str(t.get("title") or "")[:120],
            "status": t.get("status"),
            "strategy_id": t.get("strategy_id"),
            "created_at": t.get("created_at"),
            "cost_usd": t.get("cost_usd"),
            "error": (str(t.get("error"))[:160] if t.get("error") else None),
        }
        for t in rows[:cap]
    ]
    return _json.dumps({"count": len(out), "tasks": out}, indent=2, default=str)


# ---------------------------------------------------------------------------
# Action tools (operator-authorized for direct use)
# ---------------------------------------------------------------------------

@register_tool(
    name="assistant_create_strategy",
    description=(
        "Create a new tradable strategy from the operator's natural-language idea. "
        "Mints an operator hypothesis automatically, then registers the strategy in "
        "the 'quick_screen' stage so it can be backtested and run through the gauntlet. "
        f"`strategy_type` MUST be an existing family (e.g. {_COMMON_FAMILIES}) — do not "
        "invent a type. Composite param sets mixing indicators are fine. Returns the new "
        "strategy id. After creating, you may call assistant_run_backtest on it."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "idea": {
                "type": "string",
                "description": "The operator's idea / thesis in their own words (drives the hypothesis).",
            },
            "name": {"type": "string", "description": "Short human-readable strategy name."},
            "strategy_type": {
                "type": "string",
                "description": f"An existing strategy family. Common: {_COMMON_FAMILIES}.",
            },
            "symbol": {"type": "string", "description": "Asset: BTC, ETH, SOL, ..."},
            "timeframe": {"type": "string", "description": "Timeframe: 15m, 1h, 4h, 1d (default 1h)."},
            "params": {"type": "object", "description": "Strategy params dict for the chosen family."},
            "notes": {"type": "string", "description": "Optional notes about the strategy."},
        },
        "required": ["idea", "name", "strategy_type", "symbol", "params"],
    },
    permissions={"brain", None},
)
def _tool_assistant_create_strategy(
    idea: str,
    name: str,
    strategy_type: str,
    symbol: str,
    params: dict,
    timeframe: str = "1h",
    notes: str = "",
) -> str:
    from forven.brain import create_strategy, resolve_brain_provider_model
    from forven.hypotheses import create_hypothesis
    from forven.strategies.certification import certify_execution_strategy

    strategy_type = str(strategy_type or "").strip()
    symbol = str(symbol or "").strip().upper()
    timeframe = str(timeframe or "1h").strip() or "1h"
    name = str(name or "").strip() or f"{symbol} {strategy_type}"
    if not isinstance(params, dict):
        return "Error: params must be an object/dict of strategy parameters."
    if not strategy_type or not symbol:
        return "Error: strategy_type and symbol are required."

    # Certify the family/params BEFORE minting a hypothesis so an invalid type
    # never leaves an orphan hypothesis behind.
    certification = certify_execution_strategy(strategy_type, params)
    cert_error = certification.format_error(context="creation")
    if certification.unregistered_runtime_type or cert_error:
        return (
            f"Cannot create strategy: '{strategy_type}' is not a registered family or the "
            f"params are invalid. Pick an existing family ({_COMMON_FAMILIES}). "
            f"Detail: {cert_error or 'unregistered runtime type'}"
        )

    try:
        provider, model_id = resolve_brain_provider_model()
    except Exception:
        provider, model_id = ("openai", None)

    idea_text = str(idea or "").strip() or f"Operator idea for a {strategy_type} strategy on {symbol}."
    try:
        hypothesis = create_hypothesis(
            title=name[:120],
            market_thesis=idea_text[:2000],
            mechanism=(
                f"{strategy_type} signals on {symbol} {timeframe}. "
                f"Params: {_json.dumps(params)[:400]}"
            ),
            lane="benchmarking",
            source_type="operator_manual",
            origin_role="operator",
            origin_model=provider,
            origin_model_id=model_id,
            target_assets=[symbol],
            target_timeframes=[timeframe],
            novelty_score=0.0,
        )
    except Exception as exc:
        return f"Could not create the parent hypothesis: {exc}"

    hyp_id = str(hypothesis.get("id") or "").strip()
    if not hyp_id:
        return "Could not create the parent hypothesis (no id returned)."

    result = create_strategy(
        strategy_id="",
        name=name,
        strategy_type=strategy_type,
        symbol=symbol,
        params=params,
        timeframe=timeframe,
        notes=str(notes or "")[:1000],
        model=provider,
        model_id=model_id,
        hypothesis_id=hyp_id,
    )
    if not isinstance(result, dict):
        return "Error creating strategy: unexpected response."
    if result.get("error"):
        return f"Error creating strategy: {result['error']}"
    sid = str(result.get("id") or "").strip()
    stage = str(result.get("status") or result.get("stage") or "quick_screen").strip()
    if not sid:
        return "Error creating strategy: no id returned."
    return _json.dumps(
        {
            "ok": True,
            "strategy_id": sid,
            "stage": stage,
            "hypothesis_id": hyp_id,
            "name": name,
            "type": strategy_type,
            "symbol": symbol,
            "timeframe": timeframe,
            "message": (
                f"Created {sid} ({name}) in stage '{stage}'. "
                f"Run assistant_run_backtest with strategy_id={sid} to see how it performs."
            ),
        },
        indent=2,
    )


@register_tool(
    name="assistant_run_backtest",
    description=(
        "Run a LOCAL backtest for a strategy by id using its stored type/symbol/"
        "timeframe/params. Optional overrides: timeframe, bars. Returns headline metrics."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy id, e.g. S00719"},
            "timeframe": {"type": "string", "description": "Optional timeframe override."},
            "bars": {"type": "integer", "description": "Optional bar-count override."},
        },
        "required": ["strategy_id"],
    },
    permissions={"brain", None},
)
def _tool_assistant_run_backtest(
    strategy_id: str,
    timeframe: str | None = None,
    bars: int | None = None,
) -> str:
    sid = str(strategy_id or "").strip()
    if not sid:
        return "Error: strategy_id is required."
    with get_db() as conn:
        row = conn.execute(
            "SELECT type, runtime_type, symbol, timeframe, params FROM strategies WHERE id = ?",
            (sid,),
        ).fetchone()
    if not row:
        return f"No strategy found with id {sid}."
    strategy_type = row["runtime_type"] or row["type"]
    asset = row["symbol"] or "BTC"
    tf = timeframe or row["timeframe"] or "1h"
    params = _json.loads(row["params"]) if row["params"] else {}

    from forven.strategies.backtest import backtest_strategy

    result = backtest_strategy(
        strategy_id=sid,
        asset=asset,
        strategy_type=strategy_type,
        params=params,
        bars=bars,
        timeframe=tf,
        persist_legacy_run=False,
        regime_gate=False,
    )
    if result.get("error"):
        return f"Backtest error: {result['error']}"
    m = result.get("metrics", {})
    return _json.dumps(
        {
            "strategy_id": sid,
            "timeframe": tf,
            "total_trades": m.get("total_trades", 0),
            "win_rate": m.get("win_rate", 0),
            "sharpe": m.get("sharpe", 0),
            "profit_factor": m.get("profit_factor", 0),
            "max_drawdown_pct": m.get("max_drawdown_pct", 0),
            "total_return_pct": m.get("total_return_pct", 0),
            "avg_bars_held": m.get("avg_bars_held", 0),
        },
        indent=2,
    )


# ---------------------------------------------------------------------------
# Drop-zone register + enqueue-to-gauntlet (operator-authorized direct use)
#
# Lets the assistant run the discovery loop from chat: register a custom
# strategy file, then submit a candidate into the GAUNTLET (the automated
# 12-step evaluation). Enqueue enters EVALUATION only — it advances to the
# 'gauntlet' stage, never to paper/live (those stay confirm-gated via the
# promote_strategy tool, which surfaces an operator confirm card).
# ---------------------------------------------------------------------------

# Quick-screen gate thresholds (judged on BOTH the IS and OOS windows of a
# canonical backtest over the configured Backtest window). Mirrors forven/agent/client.py.
_QUICK_SCREEN = {
    "min_profit_factor": 1.05, "min_sharpe": 0.0, "max_sharpe": 5.0,
    "max_drawdown_pct": 0.30, "min_trades_oos": 15, "min_trades_is": 20,
    "min_total_return_pct": 0.0,
}


def _bt_side(result: dict, side: str) -> dict:
    """Pull an in_sample/out_of_sample block from a backtest result (handles
    both top-level and metrics-nested shapes)."""
    if isinstance(result, dict):
        if isinstance(result.get(side), dict):
            return result[side]
        m = result.get("metrics")
        if isinstance(m, dict) and isinstance(m.get(side), dict):
            return m[side]
    return {}


def _quick_screen(compact: dict) -> dict:
    """Pre-screen compact IS/OOS metrics against the quick-screen gate."""
    reasons: list[str] = []

    def num(x):
        return x if isinstance(x, (int, float)) else None

    for side, min_tr in (("in_sample", _QUICK_SCREEN["min_trades_is"]),
                         ("out_of_sample", _QUICK_SCREEN["min_trades_oos"])):
        s = compact.get(side, {}) if isinstance(compact, dict) else {}
        pf, sh, tr = num(s.get("profit_factor")), num(s.get("sharpe")), num(s.get("total_trades"))
        dd, ret = num(s.get("max_drawdown_pct")), num(s.get("total_return_pct"))
        if pf is None or pf < _QUICK_SCREEN["min_profit_factor"]:
            reasons.append(f"{side} profit_factor {pf} < {_QUICK_SCREEN['min_profit_factor']}")
        if sh is None or sh < _QUICK_SCREEN["min_sharpe"] or sh > _QUICK_SCREEN["max_sharpe"]:
            reasons.append(f"{side} sharpe {sh} out of [0,5]")
        if dd is None or dd >= _QUICK_SCREEN["max_drawdown_pct"]:
            reasons.append(f"{side} max_drawdown_pct {dd} >= 0.30")
        if tr is None or tr < min_tr:
            reasons.append(f"{side} total_trades {tr} < {min_tr}")
        if ret is None or ret < _QUICK_SCREEN["min_total_return_pct"]:
            reasons.append(f"{side} total_return_pct {ret} < 0")
    return {"pass": not reasons, "reasons": reasons}


@register_tool(
    name="assistant_register_strategy_file",
    description=(
        "Register a custom strategy .py file (already written to "
        "forven/strategies/custom/) into the 'quick_screen' stage so it can be "
        "backtested and enqueued. `file_path` must be an absolute path. Returns the "
        "new strategy id and stage. Use assistant_create_strategy instead when "
        "building from a built-in family + params (no file needed)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Absolute path to the .py in forven/strategies/custom/"},
            "session_id": {"type": "string", "description": "Optional drop-zone session id to group the work."},
        },
        "required": ["file_path"],
    },
    permissions={"brain", None},
)
def _tool_assistant_register_strategy_file(file_path: str, session_id: str | None = None) -> str:
    from forven.strategies.intake import register_custom_strategy_file

    fp = str(file_path or "").strip()
    if not fp:
        return "Error: file_path is required (absolute path to a .py in forven/strategies/custom/)."
    try:
        res = register_custom_strategy_file(
            file_path=fp, source="in_app_agent", session_id=(session_id or None)
        )
    except Exception as exc:  # ValueError for bad files; surface plainly
        return f"Could not register strategy file: {exc}"
    return _json.dumps(res, indent=2, default=str)


@register_tool(
    name="assistant_enqueue_candidate",
    description=(
        "Submit a quick_screen strategy into the GAUNTLET (the automated 12-step "
        "evaluation) after a quick pre-screen. Runs a canonical backtest over the "
        "configured Backtest window (Settings > Lab), "
        "checks BOTH the in-sample and out-of-sample windows against the quick-screen "
        "gate, and — only if it passes — advances the strategy to the 'gauntlet' stage "
        "so the background Advancer evaluates it toward paper. This enters EVALUATION "
        "ONLY; it never promotes to paper or live (that stays operator-confirmed via "
        "promote_strategy). Returns the metrics, the pre-screen verdict, and whether "
        "it was enqueued. dataset_id defaults to the strategy's own symbol+timeframe."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "strategy_id": {"type": "string", "description": "Strategy id (e.g. S00719), in quick_screen."},
            "dataset_id": {"type": "string", "description": "Optional 'SYMBOL/QUOTE-timeframe', e.g. BTC/USDT-1h."},
            "trade_mode": {"type": "string", "description": "Optional: both | long_only | short_only."},
        },
        "required": ["strategy_id"],
    },
    permissions={"brain", None},
)
def _tool_assistant_enqueue_candidate(
    strategy_id: str,
    dataset_id: str | None = None,
    trade_mode: str | None = None,
) -> str:
    from forven.api_core import post_backtesting_run
    from forven.brain import promote_strategy

    sid = str(strategy_id or "").strip()
    if not sid:
        return "Error: strategy_id is required."

    ds = str(dataset_id or "").strip()
    if not ds:
        with get_db() as conn:
            row = conn.execute(
                "SELECT symbol, timeframe FROM strategies WHERE id = ?", (sid,)
            ).fetchone()
        if not row:
            return f"No strategy found with id {sid}."
        sym = (row["symbol"] or "BTC").strip()
        tf = (row["timeframe"] or "1h").strip()
        if "-" in sym:  # already a dataset-ish string like BTC/USDT-1h
            ds = sym
        else:
            if "/" not in sym:
                sym = f"{sym}/USDT"
            ds = f"{sym}-{tf}"

    body = {"strategy_id": sid, "dataset_id": ds, "request_source": "in_app_agent"}
    if trade_mode:
        body["trade_mode"] = str(trade_mode).strip()
    try:
        result = post_backtesting_run(body)
    except Exception as exc:
        return f"Backtest failed for {sid} on {ds}: {exc}"
    if isinstance(result, dict) and result.get("error"):
        return f"Backtest error for {sid}: {result['error']}"

    keys = ("profit_factor", "sharpe", "total_trades", "max_drawdown_pct", "win_rate", "total_return_pct")
    compact = {
        "in_sample": {k: _bt_side(result, "in_sample").get(k) for k in keys},
        "out_of_sample": {k: _bt_side(result, "out_of_sample").get(k) for k in keys},
    }
    screen = _quick_screen(compact)
    out = {"strategy_id": sid, "dataset_id": ds, "metrics": compact,
           "quick_screen": screen, "enqueued": False}
    if not screen["pass"]:
        out["message"] = ("Did not pass the quick-screen pre-check; not enqueued. "
                          + "; ".join(screen["reasons"][:4]))
        return _json.dumps(out, indent=2, default=str)

    ok, msg = promote_strategy(sid, "gauntlet")
    advanced = bool(ok) or ("gauntlet" in str(msg).lower())
    out["enqueued"] = advanced
    out["promotion_message"] = msg
    out["message"] = (
        f"Enqueued {sid} into the gauntlet for automated evaluation toward paper."
        if advanced else f"Pre-screen passed but enqueue was blocked: {msg}"
    )
    return _json.dumps(out, indent=2, default=str)
