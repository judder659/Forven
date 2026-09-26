# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""MCP server that exposes the Forven AI Drop Zone as tools.

FastMCP builds the JSON-Schema for each tool from the Python type hints +
docstring, so tool definitions are just annotated functions. The server
runs over stdio by default — ideal for Claude Desktop which spawns the
process and talks to it through pipes.

Tool naming: every tool is prefixed with `forven_` so they do not collide
with other MCP servers the user has installed (common convention).

Design goal: a harness that has NEVER seen Forven should be able to walk a
strategy from idea to a genuine PAPER promotion using only the tool
descriptions and the payloads the tools return. The tool surface encodes the
loop order, sessions manage themselves (auto-open on first write, auto-tag,
auto-close on disconnect + server-side idle sweep), and the persisted
robustness suite — the evidence the paper gate actually reads — is a
first-class tool instead of background-loop folklore.
"""

from __future__ import annotations

import atexit
import logging
import ntpath
import re
from typing import Any

from mcp.server.fastmcp import FastMCP

from .client import ForvenClient

log = logging.getLogger("forven.mcp_server")


_GATE_PATTERN = re.compile(
    r"(?P<id>Gate\d+|S\d+\s+REJECT|P\d+-\d+\s+REJECT|Hard sanity check failed|Trade count|IS Sharpe|Robustness|Max drawdown|Gauntlet missing|Insufficient [^:]+)",
    re.IGNORECASE,
)

# The persisted robustness tests the paper gate reads, and how each one is
# submitted. walk_forward/cost_stress take (strategy_id, symbol, timeframe);
# param_jitter/monte_carlo/regime_split need a baseline backtest result_id.
_ROBUSTNESS_TESTS = {
    "walk_forward": {"path": "/api/robustness/walk-forward/submit", "needs": "symbol"},
    "cost_stress": {"path": "/api/robustness/cost-stress/submit", "needs": "symbol"},
    "param_jitter": {"path": "/api/robustness/param-jitter/submit", "needs": "result"},
    "monte_carlo": {"path": "/api/robustness/monte-carlo/submit", "needs": "result_only"},
    "regime_split": {"path": "/api/robustness/regime-split/submit", "needs": "result_only"},
}
_ROBUSTNESS_ALIASES = {
    "wfa": "walk_forward",
    "walkforward": "walk_forward",
    "parameter_stability": "param_jitter",
    "parameter_jitter": "param_jitter",
    "jitter": "param_jitter",
    "cost": "cost_stress",
    "montecarlo": "monte_carlo",
}
_DEFAULT_ROBUSTNESS_TESTS = ["walk_forward", "cost_stress", "param_jitter"]

# get_context returns one section at a time so a harness never has to swallow
# the full blob. 'overview' is the entry point and lists the other sections.
_CONTEXT_SECTIONS: dict[str, tuple[str, ...]] = {
    "overview": (
        "role", "description", "workspace", "file_location",
        "existing_custom_strategies", "prebuilt_families", "creative_freedom",
        "workflow", "gotchas", "sessions",
    ),
    "template": ("strategy_template", "file_location"),
    "datasets": ("available_datasets",),
    "params": ("canonical_params", "param_naming_rules", "prebuilt_families", "family_restriction"),
    "gotchas": ("gotchas", "workflow"),
    "endpoints": ("api_endpoints", "sessions"),
}


class _SessionTracker:
    """Sticky Drop Zone session state for one MCP server process.

    - The first write tool call without a session auto-opens one.
    - Every later write call tags to the active session automatically.
    - Passing an explicit session_id to any tool switches the active session
      (resume semantics) without claiming ownership of it.
    - Sessions this process opened are closed on disconnect (atexit),
      so an interrupted client never strands an 'active' session. The
      backend idle sweep is the backstop for hard kills.
    """

    def __init__(self, client: ForvenClient) -> None:
        self._client = client
        self.active: str | None = None
        self.owned: list[str] = []

    def adopt(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if sid:
            self.active = sid

    def own(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if sid:
            self.active = sid
            if sid not in self.owned:
                self.owned.append(sid)

    def release(self, session_id: str) -> None:
        sid = str(session_id or "").strip()
        if sid in self.owned:
            self.owned.remove(sid)
        if self.active == sid:
            self.active = None

    def resolve(self, explicit: str | None, *, auto_label: str) -> str | None:
        """Return the session to tag with, auto-opening one when needed."""
        sid = str(explicit or "").strip()
        if sid:
            self.adopt(sid)
            return sid
        if self.active:
            return self.active
        try:
            created = self._client.post(
                "/api/ai-dropzone/sessions",
                {"label": auto_label[:200], "actor": "mcp-auto", "objective": ""},
            )
            new_id = str((created or {}).get("id") or "").strip()
            if new_id:
                self.own(new_id)
                log.info("Auto-opened Drop Zone session %s", new_id)
                return new_id
        except Exception as exc:  # tagging is best-effort; the write must not fail
            log.warning("Could not auto-open a Drop Zone session: %s", exc)
        return None

    def close_owned_on_exit(self) -> None:
        """Best-effort close of every session this process opened (atexit)."""
        if not self.owned:
            return
        try:
            closer = ForvenClient(
                base_url=self._client.base_url,
                api_key=self._client.api_key,
                operator_key=self._client.operator_key,
                timeout=5.0,
            )
            for sid in list(self.owned):
                try:
                    closer.post(f"/api/ai-dropzone/sessions/{sid}/close")
                    log.info("Closed Drop Zone session %s on disconnect", sid)
                except Exception:
                    pass
            closer.close()
        except Exception:
            pass


def _metric_view(metrics: dict[str, Any] | None) -> dict[str, Any]:
    """Return the gate-relevant subset of a metrics object."""
    if not isinstance(metrics, dict):
        return {}
    keys = (
        "total_trades",
        "total_return_pct",
        "total_return",
        "sharpe",
        "profit_factor",
        "win_rate",
        "max_drawdown_pct",
        "max_drawdown",
        "robustness",
        "robustness_score",
        "gauntlet_score",
        "backtest_months",
        "trade_mode",
        "position_model",
    )
    return {key: metrics.get(key) for key in keys if key in metrics}


def _compact_backtest_payload(payload: Any) -> Any:
    """Strip bulky trades/equity curves while preserving gate evidence."""
    if not isinstance(payload, dict):
        return payload
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    in_sample = metrics.get("in_sample") if isinstance(metrics.get("in_sample"), dict) else {}
    out_of_sample = metrics.get("out_of_sample") if isinstance(metrics.get("out_of_sample"), dict) else metrics
    compact: dict[str, Any] = {
        "result_id": payload.get("result_id"),
        "strategy_id": payload.get("strategy_id"),
        "asset": payload.get("asset"),
        "symbol": payload.get("symbol"),
        "timeframe": payload.get("timeframe"),
        "status": payload.get("status"),
        "error": payload.get("error"),
        "bars": payload.get("bars"),
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "trade_mode": payload.get("trade_mode") or metrics.get("trade_mode"),
        "position_model": payload.get("position_model") or metrics.get("position_model"),
        "in_sample": _metric_view(in_sample),
        "out_of_sample": _metric_view(out_of_sample),
        "overall": _metric_view(metrics),
    }
    compact["trade_count"] = (
        compact["out_of_sample"].get("total_trades")
        if compact["out_of_sample"]
        else len(payload.get("trades") or [])
    )
    return {key: val for key, val in compact.items() if val not in (None, {}, [])}


def _parse_gate_failures(message: str | None) -> list[dict[str, Any]]:
    """Best-effort parser for legacy human gate messages."""
    text = str(message or "").strip()
    if not text:
        return []
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    failures = []
    for idx, part in enumerate(p.strip() for p in text.split(";") if p.strip()):
        match = _GATE_PATTERN.search(part)
        gate_id = (match.group("id") if match else f"gate_{idx + 1}").lower().replace(" ", "_")
        failures.append(
            {
                "id": gate_id,
                "message": part,
                "severity": "block" if "warn" not in part.lower() and "flag" not in part.lower() else "warning",
            }
        )
    return failures


def _with_gate_failures(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    error = payload.get("error") or payload.get("blocked_reason")
    failures = _parse_gate_failures(error)
    if failures:
        payload = dict(payload)
        payload["failed_gates"] = failures
        payload["passed"] = False
    elif payload.get("ok") is True:
        payload = dict(payload)
        payload["failed_gates"] = []
        payload["passed"] = True
    return payload


def _split_dataset_id(dataset_id: str | None) -> tuple[str | None, str | None]:
    """'BTC/USDT-1h' → ('BTC/USDT', '1h')."""
    raw = str(dataset_id or "").strip()
    if not raw or "-" not in raw:
        return (raw or None), None
    symbol, timeframe = raw.rsplit("-", 1)
    return (symbol.strip() or None), (timeframe.strip() or None)


def _normalize_robustness_tests(tests: list[str] | None) -> list[str]:
    if not tests:
        return list(_DEFAULT_ROBUSTNESS_TESTS)
    out: list[str] = []
    for name in tests:
        key = str(name or "").strip().lower().replace("-", "_")
        key = _ROBUSTNESS_ALIASES.get(key, key)
        if key in _ROBUSTNESS_TESTS and key not in out:
            out.append(key)
    return out


def build_server(client: ForvenClient | None = None) -> FastMCP:
    """Construct the FastMCP server instance with every tool registered.

    Exposed so tests can introspect the tool list without running stdio.
    """
    forven = client or ForvenClient()
    sessions = _SessionTracker(forven)
    # Close any session this process opened when the MCP client disconnects
    # (stdio EOF ends the process). No-op when nothing was opened, so test
    # instantiation never fires HTTP at interpreter exit.
    atexit.register(sessions.close_owned_on_exit)

    server = FastMCP(
        name="forven",
        instructions=(
            "Forven AI Drop Zone — design, register, backtest, validate, and "
            "promote trading strategies through the REAL lifecycle gates. "
            "The loop: (1) forven_get_context (overview first, then the "
            "'template' and 'gotchas' sections before writing any code); "
            "(2) forven_get_quant_skills for priors; (3) write a strategy .py "
            "into the workspace; (4) forven_register_strategy_file; "
            "(5) forven_run_backtest and iterate on the design; "
            "(6) forven_run_optimization, then bake winning params into the "
            "file's default_params; (7) forven_run_robustness — the PERSISTED "
            "validation suite the paper gate reads — and poll "
            "forven_get_robustness_result; (8) forven_get_gate_report, then "
            "forven_promote_strategy when every gate is green. Sessions are "
            "automatic: one opens on your first write, tags all later work, "
            "and closes when you disconnect. Never pass force=true to skip a "
            "gate — a genuine rejection is the system working."
        ),
    )

    # ── Read-only tools ────────────────────────────────────────────────

    @server.tool(
        name="forven_get_context",
        description=(
            "STEP 1 — orient. Returns one section of the Drop Zone context. "
            "Call with section='overview' (default) first: workspace paths, "
            "the workflow, and the gotchas list that encodes every "
            "registration/gate trap. Then fetch section='template' before "
            "writing a strategy file, section='datasets' to pick data, "
            "section='params' for canonical parameter families, "
            "section='endpoints' for the raw HTTP surface. section='all' "
            "returns the full blob (large)."
        ),
    )
    def forven_get_context(section: str = "overview") -> dict[str, Any]:
        blob = forven.get("/api/ai-dropzone/context")
        if not isinstance(blob, dict):
            return {"error": "context unavailable", "raw": blob}
        key = str(section or "overview").strip().lower()
        if key in ("all", "full", "*"):
            return blob
        keys = _CONTEXT_SECTIONS.get(key)
        if keys is None:
            return {
                "error": f"Unknown section '{section}'.",
                "sections": sorted(_CONTEXT_SECTIONS) + ["all"],
            }
        out = {k: blob[k] for k in keys if k in blob}
        out["section"] = key
        out["sections"] = sorted(_CONTEXT_SECTIONS) + ["all"]
        if key == "overview":
            out["next"] = (
                "Fetch section='template' before writing a strategy file. "
                "The gotchas above are hard-won — violating them wastes a "
                "registration or a gate run."
            )
        return out

    @server.tool(
        name="forven_list_sessions",
        description=(
            "List recent Drop Zone sessions with strategy counts. Sessions "
            "idle beyond the server TTL auto-close, so 'active' means "
            "genuinely in use. Pass include_closed=false for open ones only."
        ),
    )
    def forven_list_sessions(limit: int = 20, include_closed: bool = True) -> dict[str, Any]:
        return forven.get(
            "/api/ai-dropzone/sessions",
            params={"limit": limit, "include_closed": str(include_closed).lower()},
        )

    @server.tool(
        name="forven_get_session",
        description=(
            "Fetch a session's detail: tagged strategies and recent backtest "
            "runs. Defaults to this process's active session. Use it to "
            "answer 'what did I try in session X?'."
        ),
    )
    def forven_get_session(session_id: str | None = None) -> dict[str, Any]:
        sid = str(session_id or "").strip() or sessions.active
        if not sid:
            return {"error": "No active session. Pass session_id or make a write call first."}
        return forven.get(f"/api/ai-dropzone/sessions/{sid}")

    @server.tool(
        name="forven_list_strategies",
        description=(
            "List registered strategies. Filter by status ('active', "
            "'archived', etc.). Use this to see what's in the lab."
        ),
    )
    def forven_list_strategies(status: str | None = None) -> Any:
        return forven.get("/api/strategies", params={"status": status})

    @server.tool(
        name="forven_get_recent_runs",
        description="Return the last N backtest runs across all strategies.",
    )
    def forven_get_recent_runs(limit: int = 20) -> Any:
        return forven.get("/api/backtesting/runs", params={"limit": limit})

    @server.tool(
        name="forven_get_result",
        description=(
            "Fetch a backtest result by result_id — full metrics, trades, "
            "config. Use after forven_run_backtest (which returns compact "
            "metrics by default) when you need trade-level detail."
        ),
    )
    def forven_get_result(result_id: str) -> Any:
        return forven.get(f"/api/results/{result_id}")

    @server.tool(
        name="forven_get_robustness_result",
        description=(
            "STEP 7b — poll a persisted robustness run submitted by "
            "forven_run_robustness. Returns status (running/succeeded/failed) "
            "plus the verdict scorecard once done. Poll every ~15-30s; "
            "walk-forward and param-jitter can take a few minutes."
        ),
    )
    def forven_get_robustness_result(result_id: str) -> Any:
        return forven.get(f"/api/robustness/results/{result_id}")

    @server.tool(
        name="forven_get_gate_report",
        description=(
            "STEP 8 — the single status readout: lifecycle stage, latest "
            "compact backtest, promotion_ready flag, structured failed_gates "
            "(each with an actionable hint), and next_actions. Read-only; "
            "call it whenever you need to know what stands between a "
            "strategy and promotion."
        ),
    )
    def forven_get_gate_report(strategy_id: str) -> dict[str, Any]:
        container = forven.get(f"/api/strategies/{strategy_id}/container")
        result_payload: Any = None
        try:
            results = forven.get("/api/results", params={"strategy": strategy_id, "limit": 1})
            rows = results.get("results") if isinstance(results, dict) else results
            if isinstance(rows, list) and rows:
                result_id = rows[0].get("result_id") or rows[0].get("id")
                result_payload = forven.get(f"/api/results/{result_id}") if result_id else rows[0]
        except Exception as exc:
            result_payload = {"error": f"Could not fetch latest result: {exc}"}
        # Strategy-scoped, structured gate checklist (the per-strategy
        # `/events` subroute does not exist; readiness is the purpose-built
        # source and gives structured pass/fail steps directly).
        readiness: Any = None
        try:
            readiness = forven.get(f"/api/lifecycle/strategies/{strategy_id}/readiness")
        except Exception as exc:
            readiness = {"error": f"Could not fetch readiness: {exc}"}
        failed_gates: list[dict[str, Any]] = []
        if isinstance(readiness, dict):
            for step in readiness.get("steps") or []:
                if isinstance(step, dict) and str(step.get("status")).lower() == "failed":
                    failed_gates.append(
                        {
                            "id": str(step.get("name") or "gate"),
                            "message": step.get("detail") or "",
                            "severity": "block",
                            "actionable": step.get("actionable"),
                        }
                    )
        ready = readiness.get("ready") if isinstance(readiness, dict) else None
        next_actions: list[str] = []
        if ready:
            next_actions.append("All gates green — call forven_promote_strategy (force=false).")
            # Passing steps can carry a two-tier caveat (e.g. walk_forward passed
            # the paper-tier fold criteria while its strict artifact verdict is
            # FAIL). Failed steps are already surfaced; without this, the caveat
            # on a PASSED step is dropped and ready:true reads as a false green
            # next to the artifact's FAIL verdict.
            if isinstance(readiness, dict):
                for step in readiness.get("steps") or []:
                    if not isinstance(step, dict):
                        continue
                    detail = str(step.get("detail") or "")
                    if str(step.get("status")).lower() == "passed" and "strict artifact verdict" in detail:
                        next_actions.append(f"Note: {detail}")
        for gate in failed_gates:
            action = gate.get("actionable")
            if action in ("run_validation_suite", "re_run_validation_suite"):
                next_actions.append(
                    "Missing/failing persisted validation — run forven_run_robustness and poll "
                    "forven_get_robustness_result."
                )
            elif action == "run_optimization":
                next_actions.append("Run forven_run_optimization for this strategy.")
            elif action == "run_timeframe_sweep":
                next_actions.append("Multi-timeframe evidence missing — run backtests on more timeframes.")
        return {
            "strategy_id": strategy_id,
            "strategy": container.get("strategy") if isinstance(container, dict) else container,
            "latest_result": _compact_backtest_payload(result_payload),
            "promotion_ready": ready,
            "failed_gates": failed_gates,
            "latest_gate_failure": failed_gates[0] if failed_gates else None,
            "next_actions": next_actions,
        }

    @server.tool(
        name="forven_get_quant_skills",
        description=(
            "STEP 2 — load curated quant insights before designing. Past "
            "survivors and failures leave hints here; use them to avoid "
            "known dead ends, not as guaranteed edges. regime options: "
            "'trending', 'range_bound', 'volatile'."
        ),
    )
    def forven_get_quant_skills(
        regime: str | None = None,
        skill_type: str | None = None,
        limit: int = 10,
        min_confidence: float = 0.5,
    ) -> Any:
        return forven.get(
            "/api/quant-skills",
            params={
                "regime": regime,
                "skill_type": skill_type,
                "limit": limit,
                "min_confidence": min_confidence,
            },
        )

    # ── Write tools ────────────────────────────────────────────────────

    @server.tool(
        name="forven_create_session",
        description=(
            "Optional — open a labeled Drop Zone session. Usually unnecessary: "
            "a session auto-opens on your first register/backtest call and "
            "everything after tags to it automatically. Use this only to set "
            "a meaningful label/objective up front. The session closes itself "
            "when you disconnect."
        ),
    )
    def forven_create_session(
        label: str = "",
        actor: str = "claude-mcp",
        objective: str = "",
    ) -> dict[str, Any]:
        created = forven.post(
            "/api/ai-dropzone/sessions",
            {"label": label, "actor": actor, "objective": objective},
        )
        if isinstance(created, dict) and created.get("id"):
            sessions.own(str(created["id"]))
            created["note"] = (
                "This is now the active session — subsequent register/backtest "
                "calls tag to it automatically; it closes on disconnect."
            )
        return created

    @server.tool(
        name="forven_close_session",
        description=(
            "Mark a Drop Zone session closed (idempotent). Defaults to this "
            "process's active session. Also happens automatically on "
            "disconnect, so only call it to end a session early."
        ),
    )
    def forven_close_session(session_id: str | None = None) -> dict[str, Any]:
        sid = str(session_id or "").strip() or sessions.active
        if not sid:
            return {"error": "No active session to close."}
        result = forven.post(f"/api/ai-dropzone/sessions/{sid}/close")
        sessions.release(sid)
        return result

    @server.tool(
        name="forven_register_strategy_file",
        description=(
            "STEP 4 — register a strategy .py you wrote to the workspace "
            "(absolute path). Returns strategy_id + stage: quick_screen if "
            "it certifies, otherwise archived with an untestable_reason "
            "(crash, lookahead leak, failed certification) — fix the file "
            "and register it again. The strategy_id feeds every later call. "
            "Auto-tags to the active session. NOTE: re-registering a TYPE_NAME "
            "that is still active is rejected; logic edits don't need "
            "re-registration (code loads live), but changed default_params "
            "need a NEW file + TYPE_NAME."
        ),
    )
    def forven_register_strategy_file(
        file_path: str,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        # ntpath.basename handles both / and \ separators regardless of host OS.
        sid = sessions.resolve(session_id, auto_label=f"auto · register {ntpath.basename(file_path)}")
        body: dict[str, Any] = {"file_path": file_path, "source": "ai_dropzone_mcp"}
        if sid:
            body["session_id"] = sid
        try:
            return forven.post("/api/strategies/intake/register-file", body)
        except Exception as exc:
            message = str(exc)
            hint = None
            if "already registered" in message.lower():
                hint = (
                    "This file/TYPE_NAME is already registered — reuse the existing "
                    "strategy_id. Logic edits apply on the next backtest without "
                    "re-registering; to change default_params, write a NEW file with "
                    "a new TYPE_NAME."
                )
            elif "banned imports" in message.lower() or "security scan" in message.lower():
                hint = (
                    "The AST gate rejected the file. Remove banned imports (ta, os, "
                    "subprocess, ...) and dynamic exec/eval — compute indicators in "
                    "native pandas/numpy."
                )
            if hint:
                return {"ok": False, "error": message, "hint": hint}
            raise

    @server.tool(
        name="forven_run_backtest",
        description=(
            "STEP 5 — backtest a registered strategy. dataset_id is "
            "'SYMBOL-TIMEFRAME' e.g. 'BTC/USDT-1h'. Returns compact "
            "gate-relevant metrics by default (in_sample / out_of_sample); "
            "pass compact=false or use forven_get_result(result_id) for full "
            "trades. parameters overrides default_params for EXPLORATION "
            "only — gates judge the registered file's defaults, so bake "
            "winners into the file. Auto-tags to the active session."
        ),
    )
    def forven_run_backtest(
        strategy_id: str,
        dataset_id: str,
        session_id: str | None = None,
        parameters: dict[str, Any] | None = None,
        timeframe: str | None = None,
        start: str | None = None,
        end: str | None = None,
        leverage: float | None = None,
        trade_mode: str | None = None,
        compact: bool = True,
    ) -> Any:
        sid = sessions.resolve(session_id, auto_label=f"auto · backtest {strategy_id}")
        body: dict[str, Any] = {
            "strategy_id": strategy_id,
            "dataset_id": dataset_id,
            "request_source": "mcp_server",
        }
        if sid:
            body["session_id"] = sid
        if parameters is not None:
            body["parameters"] = parameters
        if timeframe:
            body["timeframe"] = timeframe
        if start:
            body["start"] = start
        if end:
            body["end"] = end
        if leverage is not None:
            body["leverage"] = leverage
        if trade_mode:
            body["trade_mode"] = trade_mode
        result = forven.post("/api/backtesting/run", body)
        if not compact:
            return result
        payload = _compact_backtest_payload(result)
        if isinstance(payload, dict) and sid:
            payload["session_id"] = sid
        return payload

    @server.tool(
        name="forven_create_strategy",
        description=(
            "Create a normal certified strategy container using a built-in "
            "execution family. This complements register-file for cases where "
            "the strategy should use an existing family such as rsi_momentum."
        ),
    )
    def forven_create_strategy(
        strategy_type: str,
        symbol: str,
        timeframe: str,
        parameters: dict[str, Any],
        name: str = "",
        hypothesis_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "type": strategy_type,
            "strategy_type": strategy_type,
            "symbol": symbol,
            "timeframe": timeframe,
            "params": parameters,
            "name": name,
        }
        if hypothesis_id:
            body["hypothesis_id"] = hypothesis_id
        return forven.post("/api/backtesting/strategies", body)

    @server.tool(
        name="forven_run_optimization",
        description=(
            "STEP 6 — parameter search for a registered strategy. Persisted "
            "optimization evidence is itself a paper-gate requirement. After "
            "it finishes, BAKE the winning params into the strategy file's "
            "default_params — gates judge the registered defaults."
        ),
    )
    def forven_run_optimization(
        strategy_id: str,
        dataset_id: str,
        parameter_ranges: dict[str, Any] | None = None,
        objective: str | None = None,
        n_trials: int | None = None,
        timeframe: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> Any:
        body: dict[str, Any] = {"strategy_id": strategy_id, "dataset_id": dataset_id}
        if parameter_ranges is not None:
            body["parameter_ranges"] = parameter_ranges
        if objective:
            body["objective"] = objective
        if n_trials is not None:
            body["n_trials"] = n_trials
        if timeframe:
            body["timeframe"] = timeframe
        if start:
            body["start"] = start
        if end:
            body["end"] = end
        return forven.post("/api/backtesting/optimize", body)

    @server.tool(
        name="forven_run_robustness",
        description=(
            "STEP 7 — submit the PERSISTED robustness suite: walk_forward, "
            "cost_stress, param_jitter by default (also accepts monte_carlo, "
            "regime_split). These write the validation artifacts the paper "
            "gate actually reads — transient probes do not count. Runs in "
            "the background: poll each returned result_id with "
            "forven_get_robustness_result, then check forven_get_gate_report. "
            "dataset_id ('BTC/USDT-1h') defaults to the strategy's stored "
            "symbol/timeframe; param_jitter needs a baseline backtest and "
            "auto-uses the strategy's latest result unless baseline_result_id "
            "is passed."
        ),
    )
    def forven_run_robustness(
        strategy_id: str,
        dataset_id: str | None = None,
        tests: list[str] | None = None,
        baseline_result_id: str | None = None,
    ) -> dict[str, Any]:
        wanted = _normalize_robustness_tests(tests)
        if not wanted:
            return {
                "error": "No recognized tests requested.",
                "available": sorted(_ROBUSTNESS_TESTS),
            }
        symbol, timeframe = _split_dataset_id(dataset_id)
        if not symbol or not timeframe:
            try:
                container = forven.get(f"/api/strategies/{strategy_id}/container")
                strat = container.get("strategy") if isinstance(container, dict) else {}
                symbol = symbol or str((strat or {}).get("symbol") or "").strip() or None
                timeframe = timeframe or str((strat or {}).get("timeframe") or "").strip() or None
            except Exception:
                pass
        baseline = str(baseline_result_id or "").strip() or None
        needs_baseline = any(_ROBUSTNESS_TESTS[t]["needs"] in ("result", "result_only") for t in wanted)
        if needs_baseline and not baseline:
            try:
                results = forven.get("/api/results", params={"strategy": strategy_id, "limit": 10})
                rows = results.get("results") if isinstance(results, dict) else results
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    rtype = str(row.get("result_type") or "backtest").strip().lower()
                    if rtype == "backtest":
                        baseline = str(row.get("result_id") or row.get("id") or "").strip() or None
                        if baseline:
                            break
            except Exception:
                pass

        submitted: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for test in wanted:
            spec = _ROBUSTNESS_TESTS[test]
            needs = spec["needs"]
            if needs == "symbol":
                if not symbol or not timeframe:
                    errors[test] = "No symbol/timeframe — pass dataset_id like 'BTC/USDT-1h'."
                    continue
                body: dict[str, Any] = {
                    "strategy_id": strategy_id,
                    "symbol": symbol,
                    "timeframe": timeframe,
                }
            elif needs == "result":
                if not baseline:
                    errors[test] = (
                        "Needs a baseline backtest result — run forven_run_backtest "
                        "first or pass baseline_result_id."
                    )
                    continue
                body = {"strategy_id": strategy_id, "result_id": baseline}
            else:  # result_only
                if not baseline:
                    errors[test] = (
                        "Needs a baseline backtest result — run forven_run_backtest "
                        "first or pass baseline_result_id."
                    )
                    continue
                body = {"result_id": baseline}
            try:
                submitted[test] = forven.post(spec["path"], body)
            except Exception as exc:
                errors[test] = str(exc)

        return {
            "strategy_id": strategy_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "baseline_result_id": baseline,
            "submitted": submitted,
            "errors": errors,
            "next": (
                "Poll forven_get_robustness_result(result_id) for each submitted "
                "test until status is no longer 'running', then call "
                "forven_get_gate_report to see the updated gate ladder."
            ),
        }

    @server.tool(
        name="forven_promote_strategy",
        description=(
            "STEP 9 — attempt a non-forced lifecycle promotion. Returns "
            "structured failed_gates when the real gate system blocks it. "
            "Leave force=false: a genuine rejection is the system working, "
            "and forced passes poison the paper roster."
        ),
    )
    def forven_promote_strategy(
        strategy_id: str,
        to_status: str,
        from_status: str | None = None,
        reason: str = "",
        force: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"to_status": to_status, "reason": reason, "force": force}
        if from_status:
            body["from_status"] = from_status
        return _with_gate_failures(forven.post(f"/api/strategies/{strategy_id}/promote", body))

    @server.tool(
        name="forven_get_paper_readiness",
        description=(
            "Read current state and latest evidence for paper readiness. This "
            "does not promote; use forven_start_paper_session to attempt paper."
        ),
    )
    def forven_get_paper_readiness(strategy_id: str) -> dict[str, Any]:
        report = forven_get_gate_report(strategy_id)
        report["target_status"] = "paper"
        return report

    @server.tool(
        name="forven_start_paper_session",
        description=(
            "FINAL STEP — promote a gauntlet strategy to PAPER through the "
            "normal lifecycle gate. Only call when forven_get_gate_report "
            "shows promotion_ready=true with empty failed_gates. Never "
            "force."
        ),
    )
    def forven_start_paper_session(
        strategy_id: str,
        reason: str = "MCP paper readiness promotion",
        force: bool = False,
    ) -> dict[str, Any]:
        return forven_promote_strategy(
            strategy_id=strategy_id,
            from_status="gauntlet",
            to_status="paper",
            reason=reason,
            force=force,
        )

    @server.tool(
        name="forven_run_gauntlet_candidate",
        description=(
            "Orchestrated shortcut for steps 5+7+8 on a promising candidate: "
            "compact persisted backtest, optional PERSISTED robustness "
            "submission (run_robustness=true), then a non-forced promotion "
            "attempt toward the gauntlet stage. Poll the returned robustness "
            "result_ids with forven_get_robustness_result, then check "
            "forven_get_gate_report for the paper hop. Never bypasses gates "
            "unless force=true is explicitly passed (don't)."
        ),
    )
    def forven_run_gauntlet_candidate(
        strategy_id: str,
        dataset_id: str,
        parameters: dict[str, Any] | None = None,
        trade_mode: str | None = None,
        session_id: str | None = None,
        run_robustness: bool = True,
        robustness_tests: list[str] | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        backtest = forven_run_backtest(
            strategy_id=strategy_id,
            dataset_id=dataset_id,
            session_id=session_id,
            parameters=parameters,
            trade_mode=trade_mode,
            compact=True,
        )
        robustness = None
        if run_robustness:
            baseline = backtest.get("result_id") if isinstance(backtest, dict) else None
            robustness = forven_run_robustness(
                strategy_id=strategy_id,
                dataset_id=dataset_id,
                tests=robustness_tests,
                baseline_result_id=baseline,
            )
        promotion = forven_promote_strategy(
            strategy_id=strategy_id,
            from_status="quick_screen",
            to_status="gauntlet",
            reason="MCP gauntlet candidate evaluation",
            force=force,
        )
        return {"backtest": backtest, "robustness": robustness, "promotion": promotion}

    return server


def main() -> None:
    """Entry point: build the server and run it over stdio."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    server = build_server()
    server.run()  # defaults to stdio
