"""Built-in application guide for the in-app assistant.

Single source of truth for what the Forven app can do, page by page, plus
step-by-step walkthroughs for common operator tasks and the domain concepts
behind them. Consumed two ways:

  * ``render_app_map()`` — a compact one-line-per-page map injected into the
    assistant system prompt so the model always knows what exists and where.
  * ``get_app_guide`` tool (tools_assistant.py) — on-demand detail: full page
    entries, how-to walkthroughs, and concept explainers, so walkthrough depth
    never bloats the per-turn prompt.

``is_valid_app_route`` also gates the assistant's ``open_app_page`` navigation
tool: only routes that exist in this map (or match a known parameterized
pattern) may be opened — fail closed on anything else.

Keep this file in sync with the frontend routes (frontend/src/routes/) when
pages are added or renamed.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pages — every user-facing route, what it's for, and its main actions.
# ---------------------------------------------------------------------------

PAGES: list[dict] = [
    {
        "route": "/",
        "name": "Dashboard",
        "kind": "dashboard",
        "summary": "Live operations overview: KPIs, system pulse, data integrity, alerts feed, scheduler watch, agent activity, pipeline health.",
        "actions": [
            "Scan headline KPIs and the equity/exposure strip",
            "Read the alerts feed and data-integrity panel",
            "Watch scheduler jobs and agent activity at a glance",
        ],
    },
    {
        "route": "/data",
        "name": "Data Manager",
        "kind": "data_engine",
        "summary": "Historical market data: download feeds, inspect coverage, backfill gaps, upload CSVs, manage the perp research universe.",
        "actions": [
            "Fetch a new symbol/timeframe feed",
            "View coverage and ingestion runs; backfill gaps",
            "Seed or refresh the perp research universe",
            "Upload CSV datasets",
        ],
    },
    {
        "route": "/strategy-creator",
        "name": "Strategy Creator",
        "kind": "strategy_creator",
        "summary": "Visual rule builder: author or edit a strategy without writing code, then send it into the pipeline.",
        "actions": [
            "Build entry/exit rules visually",
            "Open and edit an existing strategy",
            "Import a strategy export",
            "Save, then 'Send to Forge →' to enter the pipeline",
        ],
    },
    {
        "route": "/hypotheses",
        "name": "The Crucible",
        "kind": "hypotheses",
        "summary": "Trading ideas (hypotheses) under test: add ideas manually or from a URL, run discovery, generate strategies from an idea.",
        "actions": [
            "Add a manual idea or harvest one from a URL",
            "Run idea discovery",
            "Generate strategies from a crucible",
            "Bulk archive/trash/restore; run LLM triage/cleanup",
        ],
    },
    {
        "route": "/hypotheses/data-gaps",
        "name": "Data Gaps",
        "kind": "hypotheses",
        "summary": "Leaderboard of missing data inputs that block better validation or execution.",
        "actions": ["Review which data gaps matter most"],
    },
    {
        "route": "/backtest/new",
        "name": "Manual Backtest",
        "kind": "backtest",
        "summary": "Configure and run a one-off backtest and view its result summary.",
        "actions": ["Pick strategy/symbol/timeframe/window and run", "Review the result summary"],
    },
    {
        "route": "/lab",
        "name": "The Forge",
        "kind": "lab",
        "summary": "The strategy pipeline board across lifecycle stages (quick_screen → gauntlet → paper → live) with gate status per strategy.",
        "actions": [
            "Browse/sort strategies by stage, trades, robustness, gate status",
            "Move a strategy to another stage (gated)",
            "Open a strategy's detail page",
        ],
    },
    {
        "route": "/lab/strategy/{id}",
        "name": "Strategy detail",
        "kind": "strategy_detail",
        "summary": "One strategy container: metrics, equity, params, robustness results, gate report, lifecycle actions, export/import, Pine script.",
        "actions": [
            "Review metrics/equity and robustness accordions",
            "Edit params, rename the display name",
            "Run lifecycle actions (stage control)",
            "Export, import, or view TradingView Pine v6",
        ],
    },
    {
        "route": "/lab/backtests",
        "name": "All Backtests",
        "kind": "lab",
        "summary": "Table of every backtest run, sortable by return/CAGR.",
        "actions": ["Sort and inspect past backtest runs"],
    },
    {
        "route": "/risk",
        "name": "Risk Command",
        "kind": "risk",
        "summary": "Emergency controls: emergency halt (close all + halt), kill-switch toggle/reset, equity/HWM re-baseline, live portfolio budget and held-pair correlations.",
        "actions": [
            "EMERGENCY HALT: close all positions and halt trading",
            "Toggle or reset the kill-switch",
            "Re-baseline equity/HWM to a fresh wallet reading",
            "Review live budget usage and pair correlations",
        ],
    },
    {
        "route": "/paper-trades",
        "name": "Paper Trades",
        "kind": "paper_trading",
        "summary": "Paper-stage sessions: open positions, recent trades, and full manual position controls (simulated fills only).",
        "actions": [
            "View open positions and recent trades per paper session",
            "Manually open/close/partial-close/flip a position",
            "Set or edit stop-loss and take-profit",
        ],
    },
    {
        "route": "/live-trades",
        "name": "Live Trades",
        "kind": "live_trading",
        "summary": "Deployed/graduated strategies: open positions, recent trades, and full manual position controls — actions drive REAL exchange orders.",
        "actions": [
            "View open positions and recent trades per live strategy",
            "Manually open/close/partial-close/flip a REAL position",
            "Set or edit stop-loss and take-profit",
        ],
    },
    {
        "route": "/all-trades",
        "name": "All Trades",
        "kind": "paper_trading",
        "summary": "Cross-strategy trade blotter; jump into a strategy's session.",
        "actions": ["Browse all trades across strategies"],
    },
    {
        "route": "/bot-factory",
        "name": "Bot Factory",
        "kind": "bot_factory",
        "summary": "Autonomous LLM trading bots (paper by default; live only behind typed GO LIVE arming). Separate from pipeline strategies.",
        "actions": ["Create a bot (from template or strategy)", "List/start/stop bots", "Kill-all bots"],
    },
    {
        "route": "/bot-factory/{id}",
        "name": "Bot detail",
        "kind": "bot_factory",
        "summary": "One bot: start/stop/clone, GO LIVE / go-paper, wallet assignment, positions, decisions, versions & diffs, stats.",
        "actions": [
            "Start/stop/clone the bot",
            "Arm GO LIVE (typed confirmation + notional ceiling) or return to paper",
            "Assign a wallet/sub-account",
            "Review decisions, versions, and stats",
        ],
    },
    {
        "route": "/bot-factory/editor",
        "name": "Bot template editor",
        "kind": "bot_factory",
        "summary": "Create, edit, and delete bot templates.",
        "actions": ["Author or edit a bot template"],
    },
    {
        "route": "/agents",
        "name": "Agent Hub",
        "kind": "agents",
        "summary": "The agent roster and its plumbing: tasks, providers & API keys, models, routing & fallbacks, schedules, health, agent terminal.",
        "actions": [
            "Manage the roster and each agent's model",
            "Add provider API keys / OAuth; watch spend and health",
            "Set model routing and fallbacks",
        ],
    },
    {
        "route": "/agents/toolsets",
        "name": "Toolset matrix",
        "kind": "agents",
        "summary": "Per-agent × per-context tool-grant matrix.",
        "actions": ["Grant or revoke tools per agent and context"],
    },
    {
        "route": "/brain",
        "name": "Brain",
        "kind": "brain",
        "summary": "The autonomous orchestrator: autonomy state, blockers and memory (Overview), decision ledger (Decisions), short-term notes (Working Notes).",
        "actions": ["Review what the Brain saw/decided/spawned", "Edit working notes"],
    },
    {
        "route": "/approval",
        "name": "Approvals",
        "kind": "approvals",
        "summary": "Human-gate queue for sensitive actions: approve/deny/revise/handoff, bulk-approve, per-type context.",
        "actions": ["Approve, deny, or revise pending gated actions", "Bulk-approve"],
    },
    {
        "route": "/diagnostics",
        "name": "Diagnostics",
        "kind": "diagnostics",
        "summary": "System self-check snapshot; re-run all checks; view/resume resumable tasks.",
        "actions": ["Re-run self-checks", "Resume interrupted tasks"],
    },
    {
        "route": "/pipeline",
        "name": "Pipeline",
        "kind": "pipeline",
        "summary": "Background processes, scheduler jobs, and autopilot status.",
        "actions": ["Watch background jobs and scheduler state"],
    },
    {
        "route": "/routines",
        "name": "Routines",
        "kind": "routines",
        "summary": "Scheduled recurring agent jobs that post results to Discord channels; plain-English local-time schedule builder.",
        "actions": ["Create/edit a routine", "Pause/resume/run-now/preview", "Pick its Discord delivery channel"],
    },
    {
        "route": "/integrations",
        "name": "Integrations",
        "kind": "integrations",
        "summary": "AI Clients (the AI Drop Zone for connecting external agents over MCP) and Agent Tool Servers tabs.",
        "actions": ["Connect an external AI client via the Drop Zone", "Jump to Agent Tool Servers"],
    },
    {
        "route": "/integrations/mcp",
        "name": "Agent Tool Servers",
        "kind": "integrations",
        "summary": "Register/configure/test/remove outbound MCP tool servers and manage per-agent grants.",
        "actions": ["Register or test an MCP server", "Grant servers to agents"],
    },
    {
        "route": "/settings",
        "name": "Settings",
        "kind": "settings",
        "summary": "Sectioned settings: home, data, lab (backtest window + gate thresholds/presets), trading, hyperliquid, notifications, system, danger zone.",
        "actions": [
            "Tune pipeline gate thresholds (presets: relaxed/default/strict — every threshold editable)",
            "Set the Lab backtest window",
            "Configure Hyperliquid credentials and Discord notifications",
            "Danger zone: factory reset",
        ],
    },
    {
        "route": "/settings/approvals",
        "name": "Approval Modes",
        "kind": "settings",
        "summary": "Per-approval-type policy: smart / off / manual.",
        "actions": ["Set how much human gating each action type needs"],
    },
    {
        "route": "/settings/profile",
        "name": "Operator profile",
        "kind": "settings",
        "summary": "Operator identity and profile settings.",
        "actions": ["Edit the operator profile"],
    },
    {
        "route": "/tasks",
        "name": "Task queue",
        "kind": "tasks",
        "summary": "Agent task queue and task containers.",
        "actions": ["Review, assign, or dismiss agent tasks"],
    },
    {
        "route": "/tasks/{id}",
        "name": "Task detail",
        "kind": "tasks",
        "summary": "One agent task: audit trail, transcript, cost/tokens/provider-model.",
        "actions": ["Read the task transcript and audit trail"],
    },
]


# ---------------------------------------------------------------------------
# How-to walkthroughs — step-by-step guides for common operator goals.
# ---------------------------------------------------------------------------

HOWTOS: dict[str, dict] = {
    "create-strategy": {
        "title": "Create a new strategy",
        "steps": [
            "Fastest: tell me the idea right here — I can create it (assistant_create_strategy) and backtest it immediately.",
            "Visual: Strategy Creator (/strategy-creator) → build entry/exit rules → Save → 'Send to Forge →'.",
            "Idea-first: add the idea in The Crucible (/hypotheses) and use 'generate strategies' so it stays linked to its hypothesis.",
            "All paths land the strategy in the quick_screen stage of The Forge (/lab).",
        ],
        "routes": ["/strategy-creator", "/hypotheses", "/lab"],
    },
    "run-backtest": {
        "title": "Backtest a strategy",
        "steps": [
            "Ask me here — I run local backtests directly and report headline metrics.",
            "Manual: /backtest/new to configure a one-off run.",
            "From a strategy's detail page (/lab/strategy/{id}) you can run and review results in place.",
            "Every past run is listed under All Backtests (/lab/backtests).",
        ],
        "routes": ["/backtest/new", "/lab/backtests"],
    },
    "advance-to-gauntlet": {
        "title": "Get a strategy into the gauntlet (automated evaluation)",
        "steps": [
            "The quick-screen gate must pass on BOTH the in-sample and out-of-sample windows of a canonical backtest over the configured Backtest window (Settings › Lab).",
            "Ask me to enqueue it — I pre-screen and advance it (assistant_enqueue_candidate); this is evaluation only, never paper/live.",
            "Or use the stage control on The Forge / strategy detail.",
            "The gauntlet then runs automatically: backtest → optimization → robustness suite (walk-forward, Monte Carlo, param jitter, cost stress, regime split) → verdict.",
        ],
        "routes": ["/lab"],
    },
    "promote-to-paper": {
        "title": "Promote a strategy to paper trading",
        "steps": [
            "The paper gate reads the PERSISTED robustness verdict from the gauntlet — the strategy must have passed it.",
            "Check what's blocking: ask me for the gate report, or open the strategy detail page's gate/robustness sections.",
            "Gate thresholds are editable in Settings › Lab (presets: relaxed/default/strict).",
            "Promotion from chat is confirm-gated — I propose it and you approve the card. Or use the stage control in The Forge.",
        ],
        "routes": ["/lab", "/settings"],
    },
    "go-live": {
        "title": "Take a strategy live",
        "steps": [
            "Hard requirements: positive backtested expectancy AND successful paper trading — no exceptions.",
            "Check readiness: ask me for the gate report toward live.",
            "Going live requires operator approval plus typed 'GO LIVE' arming with a per-strategy notional ceiling.",
            "Assign a named wallet/sub-account so live capital is isolated (Settings › Hyperliquid / wallet picker at arming).",
            "Live opens are further gated by the portfolio budget: risk caps, asset/group exposure limits, and correlation-weighted exposure (see /risk).",
        ],
        "routes": ["/lab", "/risk", "/settings"],
    },
    "manage-risk": {
        "title": "Manage live risk / stop everything",
        "steps": [
            "Risk Command (/risk) is the emergency page.",
            "EMERGENCY HALT immediately closes all positions and halts trading.",
            "The kill-switch fires automatically at 10% drawdown; 5% daily loss limit and 2% max risk per trade always apply. You can toggle/reset it here.",
            "Re-baseline equity/HWM to a fresh wallet reading if the baseline is stale.",
            "Ask me for portfolio status any time — I read live equity, drawdown, and open positions.",
        ],
        "routes": ["/risk"],
    },
    "manual-trade-controls": {
        "title": "Manually control a position (paper or live)",
        "steps": [
            "Open Paper Trades (/paper-trades) for paper sessions, or Live Trades (/live-trades) for deployed strategies.",
            "Per position: close, partial-close, flip, or edit stop-loss / take-profit.",
            "You can also open a manual position. Live Trades drives REAL exchange orders — treat it accordingly.",
        ],
        "routes": ["/paper-trades", "/live-trades"],
    },
    "add-data": {
        "title": "Add or fix market data",
        "steps": [
            "Data Manager (/data): fetch a new symbol/timeframe feed, or backfill gaps in an existing one.",
            "Upload CSVs for data the fetchers don't cover.",
            "Seed/refresh the perp research universe to widen discovery.",
            "Coverage is also self-healing: backtests demand-backfill what they need, and a scheduled job keeps series current.",
            "The Data Gaps page (/hypotheses/data-gaps) ranks which missing inputs matter most.",
        ],
        "routes": ["/data", "/hypotheses/data-gaps"],
    },
    "bot-factory": {
        "title": "Run an autonomous trading bot",
        "steps": [
            "Bot Factory (/bot-factory): create a bot from a template (or from an existing strategy).",
            "Bots run on PAPER by default.",
            "To go live: bot detail → GO LIVE — typed confirmation, a notional ceiling, and a wallet/sub-account assignment are required.",
            "Watch its decisions, versions & diffs, and stats on the bot detail page. Kill-all is on the Bot Factory page.",
        ],
        "routes": ["/bot-factory", "/bot-factory/editor"],
    },
    "routines": {
        "title": "Schedule a recurring agent job (routine)",
        "steps": [
            "Routines (/routines) → create: describe the job, set the schedule with the plain-English local-time builder.",
            "Pick the Discord channel the results post to (bot-delivered).",
            "Use preview to sanity-check output, run-now to test, pause/resume as needed.",
        ],
        "routes": ["/routines"],
    },
    "approvals": {
        "title": "Handle approvals (human gates)",
        "steps": [
            "Pending gated actions queue at /approval — approve, deny, revise, or hand off; bulk-approve is available.",
            "Tune how much gating each action type needs at /settings/approvals: smart / off / manual.",
        ],
        "routes": ["/approval", "/settings/approvals"],
    },
    "agents-and-models": {
        "title": "Configure agents, models, and API keys",
        "steps": [
            "Agent Hub (/agents): the roster tab lists every agent; set each one's provider/model.",
            "Providers & Keys tab: add API keys or OAuth for providers; watch spend and health.",
            "Routing & Fallbacks tab sets the global model policy.",
            "Tool grants per agent live at /agents/toolsets.",
            "My own model is whatever the 'brain' agent is set to — change it there.",
        ],
        "routes": ["/agents", "/agents/toolsets"],
    },
    "connect-external-ai": {
        "title": "Connect an external AI client (MCP)",
        "steps": [
            "Integrations (/integrations) → AI Clients: the AI Drop Zone lets an external agent (e.g. Claude) connect over MCP to design, backtest, validate, and promote strategies through the real gates.",
            "Sessions self-manage: one opens on the client's first write and closes on disconnect.",
            "Agent Tool Servers (/integrations/mcp) is the outbound side: register MCP servers that YOUR internal agents may call, and grant them per agent.",
        ],
        "routes": ["/integrations", "/integrations/mcp"],
    },
    "configure-settings": {
        "title": "Find your way around Settings",
        "steps": [
            "Settings (/settings) is sectioned: home, data, lab, trading, hyperliquid, notifications, system, danger zone.",
            "Lab: the backtest window and every pipeline gate threshold (presets relaxed/default/strict are just premades — everything is editable).",
            "Trading: execution mode and risk knobs. Hyperliquid: credentials and wallets.",
            "Notifications: Discord channels and per-category toggles (with a test button).",
            "Danger zone: factory reset. Approval policies live at /settings/approvals.",
        ],
        "routes": ["/settings", "/settings/approvals", "/settings/profile"],
    },
    "crucibles": {
        "title": "Work with ideas in The Crucible",
        "steps": [
            "Add an idea manually or harvest one from a URL (/hypotheses).",
            "Run discovery to source new ideas automatically.",
            "On a crucible's detail page: queue research, ask for a verdict memo, or generate strategies from it.",
            "Bulk archive/trash/restore and LLM triage keep the list clean.",
        ],
        "routes": ["/hypotheses"],
    },
    "notifications": {
        "title": "Set up notifications",
        "steps": [
            "Settings › Notifications: Discord delivery, per-category toggles, and a test button.",
            "Routines each pick their own Discord channel.",
            "In-app: sidebar badges and toasts surface approvals, trade events, and alerts as they happen.",
        ],
        "routes": ["/settings"],
    },
    "diagnose": {
        "title": "Diagnose a problem",
        "steps": [
            "Diagnostics (/diagnostics): run the self-check snapshot; resume interrupted tasks.",
            "Pipeline (/pipeline): background processes and scheduler jobs — is work actually flowing?",
            "Dashboard alerts feed and Data Integrity panel surface active issues.",
            "Ask me for an ops overview — I read system mode, kill-switch, scheduler, approvals, and recent alerts in one shot.",
        ],
        "routes": ["/diagnostics", "/pipeline", "/"],
    },
    "export-import": {
        "title": "Export, import, or share a strategy",
        "steps": [
            "Strategy detail (/lab/strategy/{id}) → Export produces a shareable bundle.",
            "Import accepts a bundle on the same page (or via Strategy Creator). Imports are sandbox-guarded — spec/params only by default.",
            "The Pine v6 view renders the strategy for TradingView.",
        ],
        "routes": ["/lab"],
    },
    "why-blocked": {
        "title": "Why isn't my strategy advancing?",
        "steps": [
            "Ask me for its gate report — it lists exactly which gates pass/fail toward the next stage.",
            "Common causes: too few trades, failed walk-forward/robustness, missing data coverage for its symbol/timeframe, or an errored validation that needs a re-run.",
            "The strategy detail page shows gauntlet status and robustness results per test.",
            "Thresholds too strict for research? Settings › Lab — use the relaxed preset or edit individual gates.",
        ],
        "routes": ["/lab", "/settings"],
    },
}


# ---------------------------------------------------------------------------
# Concepts — the domain vocabulary behind the app.
# ---------------------------------------------------------------------------

CONCEPTS: dict[str, dict] = {
    "lifecycle": {
        "title": "Strategy lifecycle stages",
        "body": (
            "quick_screen (fresh draft; must pass a quick backtest screen) → gauntlet "
            "(automated evaluation suite) → paper (simulated trading with real market data) → "
            "live_graduated (real money on an isolated wallet). Terminal states: archived, rejected, "
            "backtest_failed. Transitions are enforced by promotion gates; going live also needs "
            "operator approval."
        ),
    },
    "gauntlet": {
        "title": "The gauntlet",
        "body": (
            "The automated evaluation a strategy must survive before paper: backtest → optimization → "
            "robustness suite (walk-forward, Monte Carlo, parameter jitter, cost stress, regime split) → "
            "verdict. Results are PERSISTED artifacts — the paper gate reads the stored robustness "
            "verdict, not a fresh opinion. Scope note: Monte Carlo bootstraps the realized trades, so it "
            "tests path/sequencing stability and tail risk — NOT whether the edge is real; that is "
            "walk-forward's and regime-split's job."
        ),
    },
    "gates": {
        "title": "Promotion gates & presets",
        "body": (
            "Every stage transition is gated on metrics (trades, profit factor, Sharpe, drawdown, "
            "robustness). Presets — relaxed / default / strict — are premade threshold sets; every "
            "individual threshold is editable in Settings › Lab. Philosophy: achievable paper, strict live."
        ),
    },
    "kill-switch": {
        "title": "Kill-switch & hard risk rules",
        "body": (
            "Non-negotiable: 10% drawdown kill-switch, 5% daily loss limit, 2% max risk per trade. "
            "The kill-switch halts trading automatically; Risk Command (/risk) has the manual "
            "emergency halt (close all + halt) and kill-switch reset."
        ),
    },
    "regime": {
        "title": "Market regime",
        "body": (
            "Forven classifies the market regime per asset and can gate strategy activity on it "
            "(strict regime gating + minimum confidence are settings). The current regime shows on the "
            "dashboard and is available to me on demand."
        ),
    },
    "brain": {
        "title": "The Brain",
        "body": (
            "The autonomous orchestrator: it observes system state on a cycle, makes decisions "
            "(logged to a ledger you can audit at /brain), and spawns agent tasks. The in-app chat "
            "assistant (me) runs under the same identity but only acts when you ask."
        ),
    },
    "crucible": {
        "title": "Crucibles (hypotheses)",
        "body": (
            "A crucible is a trading idea under test — every strategy is born from one, so "
            "performance evidence flows back to the idea. Managed at /hypotheses."
        ),
    },
    "wallets": {
        "title": "Wallets & capital isolation",
        "body": (
            "Named Hyperliquid wallets/sub-accounts isolate capital: pipeline strategies and Bot "
            "Factory bots trade from their assigned wallets, never the master. Live equity is "
            "computed books-only from those wallets."
        ),
    },
    "paper-parity": {
        "title": "Paper ↔ backtest parity",
        "body": (
            "Paper trading runs the same execution kernel as backtests (same sizing — default 1% "
            "portfolio risk — same exits), so paper results are evidence, not vibes. Live fills at the "
            "current mark; paper mirrors it with live-tick stop/TP fills."
        ),
    },
    "bots-vs-strategies": {
        "title": "Bots vs pipeline strategies",
        "body": (
            "Pipeline strategies are rule-based and graduate through gates in The Forge. Bot Factory "
            "bots are autonomous LLM traders that reason per decision — separate lifecycle, separate "
            "wallets, paper by default, typed GO LIVE arming for live."
        ),
    },
}


# ---------------------------------------------------------------------------
# Rendering / lookup / route validation
# ---------------------------------------------------------------------------

_PARAM_ROUTE_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"^/lab/strategy/[^/?#]+$"),
    re.compile(r"^/hypotheses/[^/?#]+$"),
    re.compile(r"^/bot-factory/[^/?#]+$"),
    re.compile(r"^/tasks/[^/?#]+$"),
    re.compile(r"^/integrations/mcp/[^/?#]+$"),
)

_STATIC_ROUTES: frozenset[str] = frozenset(
    p["route"] for p in PAGES if "{" not in p["route"]
)

_QUERY_RE = re.compile(r"^[\w.=&%-]*$")


def is_valid_app_route(route: str) -> bool:
    """True only for in-app routes we know exist. Fail closed on anything else."""
    raw = str(route or "").strip()
    if not raw.startswith("/") or "://" in raw or raw.startswith("//"):
        return False
    path, _, query = raw.partition("?")
    if query and not _QUERY_RE.match(query):
        return False
    path = path.rstrip("/") or "/"
    if path in _STATIC_ROUTES:
        return True
    return any(rx.match(path) for rx in _PARAM_ROUTE_PATTERNS)


def render_app_map() -> str:
    """Compact one-line-per-page map for the system prompt."""
    lines = ["# APP MAP (pages you can guide the user to / open with open_app_page)"]
    for p in PAGES:
        lines.append(f"- {p['name']} ({p['route']}): {p['summary']}")
    lines.append(
        "\nFor step-by-step walkthroughs, concepts, or full page detail, call get_app_guide "
        "(topics include: " + ", ".join(sorted(HOWTOS)) + ")."
    )
    return "\n".join(lines)


def _render_page(p: dict) -> str:
    out = [f"## {p['name']} ({p['route']})", p["summary"], "", "What you can do here:"]
    out += [f"- {a}" for a in p.get("actions", [])]
    return "\n".join(out)


def _render_howto(slug: str, h: dict) -> str:
    out = [f"## How to: {h['title']}"]
    out += [f"{i}. {s}" for i, s in enumerate(h["steps"], 1)]
    if h.get("routes"):
        out.append("\nRelevant pages: " + ", ".join(h["routes"]))
    return "\n".join(out)


def _render_concept(slug: str, c: dict) -> str:
    return f"## {c['title']}\n{c['body']}"


def render_guide_index() -> str:
    lines = ["# FORVEN APP GUIDE — INDEX", "", "## Pages"]
    lines += [f"- {p['route']} — {p['name']}: {p['summary']}" for p in PAGES]
    lines += ["", "## How-to walkthroughs (pass the slug as `topic`)"]
    lines += [f"- {slug}: {h['title']}" for slug, h in HOWTOS.items()]
    lines += ["", "## Concepts (pass the slug as `topic`)"]
    lines += [f"- {slug}: {c['title']}" for slug, c in CONCEPTS.items()]
    return "\n".join(lines)


def lookup_guide(topic: str) -> str:
    """Best-effort match of ``topic`` against howtos, concepts, and pages."""
    q = str(topic or "").strip().lower()
    if not q:
        return render_guide_index()

    if q in HOWTOS:
        return _render_howto(q, HOWTOS[q])
    if q in CONCEPTS:
        return _render_concept(q, CONCEPTS[q])

    # Route or page-name match.
    for p in PAGES:
        if q == p["route"].lower() or q == p["name"].lower() or q == p["kind"]:
            return _render_page(p)

    # Substring fallback across everything; collect up to 3 hits.
    hits: list[str] = []
    for slug, h in HOWTOS.items():
        if q in slug or q in h["title"].lower() or any(q in s.lower() for s in h["steps"]):
            hits.append(_render_howto(slug, h))
    for slug, c in CONCEPTS.items():
        if q in slug or q in c["title"].lower() or q in c["body"].lower():
            hits.append(_render_concept(slug, c))
    for p in PAGES:
        if q in p["name"].lower() or q in p["summary"].lower() or q in p["route"].lower():
            hits.append(_render_page(p))
    if hits:
        return "\n\n---\n\n".join(hits[:3])

    return "No guide entry matched " + repr(topic) + ".\n\n" + render_guide_index()
