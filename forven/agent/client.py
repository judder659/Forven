"""Zero-dependency HTTP client + high-level harness for the Forven backend.

Targets the same REST API the MCP server and the Svelte/Tauri frontend use
(default http://127.0.0.1:8003). Stdlib only (urllib) so it runs in any Python
3.8+ environment an AI harness might have — no httpx/requests needed.

Endpoint map mirrors forven/mcp_server/server.py (the proven set):
  GET  /api/health
  GET  /api/ai-dropzone/context
  GET  /api/quant-skills
  GET  /api/ai-dropzone/sessions[/{id}]
  POST /api/ai-dropzone/sessions[/{id}/close]
  GET  /api/strategies                         (?status=)
  GET  /api/strategies/{id}/container
  GET  /api/lifecycle/strategies/{id}/readiness
  GET  /api/data/readiness/strategy/{id}
  POST /api/data/readiness
  GET  /api/backtesting/runs                    (?limit=)
  GET  /api/results/{id}                        (and ?strategy=&limit=)
  POST /api/strategies/intake/register-file
  POST /api/backtesting/run
  POST /api/backtesting/optimize
  POST /api/backtesting/verdict/run
  POST /api/strategies/{id}/promote
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable

DEFAULT_BASE_URL = "http://127.0.0.1:8003"

# Lifecycle stage names (pass as to_status to promote()).
STAGE_QUICK_SCREEN = "quick_screen"
STAGE_GAUNTLET = "gauntlet"
STAGE_PAPER = "paper"

# Quick-screen gate thresholds (judged on BOTH the in-sample and out-of-sample
# windows of a 365-day backtest). These mirror the live pipeline config; treat
# them as guidance for pre-screening before you enqueue into the gauntlet.
QUICK_SCREEN_THRESHOLDS = {
    "min_profit_factor": 1.05,   # gate floor; aim >=1.3 ("fitness")
    "min_sharpe": 0.0,           # both windows; cost_stress later needs ~0.3+
    "max_sharpe": 5.0,           # leak guard
    "max_drawdown_pct": 0.30,
    "min_trades_oos": 15,
    "min_trades_is": 20,
    "min_total_return_pct": 0.0,
}


class ForvenAPIError(RuntimeError):
    """Raised on a non-2xx response. Carries status code and response body."""

    def __init__(self, status: int, body: str, method: str, path: str):
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:500]}")


class ForvenAgentClient:
    """Blocking, stdlib-only client for the Forven REST API.

    Args:
        base_url: backend origin (default env FORVEN_API_URL or :8003).
        api_key / operator_key: optional auth headers (env FORVEN_API_KEY /
            FORVEN_OPERATOR_KEY). Not needed for local calls; required only if
            the backend is exposed beyond localhost with auth enabled.
        timeout: per-request seconds (backtests are slow; default 300).
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        operator_key: str | None = None,
        timeout: float = 300.0,
    ) -> None:
        self.base_url = (base_url or os.environ.get("FORVEN_API_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("FORVEN_API_KEY") or ""
        self.operator_key = operator_key or os.environ.get("FORVEN_OPERATOR_KEY") or ""
        self.timeout = float(timeout)

    # ── transport ──────────────────────────────────────────────────────
    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None, timeout: float | None = None) -> Any:
        url = self.base_url + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"accept": "application/json"}
        if data is not None:
            headers["content-type"] = "application/json"
        if self.api_key:
            headers["x-api-key"] = self.api_key
        if self.operator_key:
            headers["x-operator-key"] = self.operator_key
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw.decode()) if raw else None
        except urllib.error.HTTPError as exc:
            raise ForvenAPIError(exc.code, exc.read().decode(errors="replace"), method, path) from None

    def get(self, path: str, params: dict | None = None, timeout: float | None = None) -> Any:
        return self._request("GET", path, params=params, timeout=timeout)

    def post(self, path: str, body: dict | None = None, timeout: float | None = None) -> Any:
        return self._request("POST", path, body=body or {}, timeout=timeout)

    # ── read tools ─────────────────────────────────────────────────────
    def health(self) -> Any:
        return self.get("/api/health", timeout=15)

    def get_context(self) -> Any:
        return self.get("/api/ai-dropzone/context")

    def get_quant_skills(self, regime: str | None = None, skill_type: str | None = None,
                         limit: int = 10, min_confidence: float = 0.5) -> Any:
        return self.get("/api/quant-skills", params={
            "regime": regime, "skill_type": skill_type,
            "limit": limit, "min_confidence": min_confidence})

    def list_sessions(self, limit: int = 20, include_closed: bool = True) -> Any:
        return self.get("/api/ai-dropzone/sessions",
                        params={"limit": limit, "include_closed": str(include_closed).lower()})

    def get_session(self, session_id: str) -> Any:
        return self.get(f"/api/ai-dropzone/sessions/{session_id}")

    def list_strategies(self, status: str | None = None) -> Any:
        return self.get("/api/strategies", params={"status": status})

    def get_strategy(self, strategy_id: str) -> Any:
        """Full container for a strategy (includes status/stage/params/metrics)."""
        return self.get(f"/api/strategies/{strategy_id}/container")

    def get_recent_runs(self, limit: int = 20) -> Any:
        return self.get("/api/backtesting/runs", params={"limit": limit})

    def get_result(self, result_id: str) -> Any:
        return self.get(f"/api/results/{result_id}")

    def data_census(self, stream: str | None = None, limit_worst: int = 50) -> Any:
        """Freshness SLA census of every stored series (GET /api/data/sla)."""
        return self.get("/api/data/sla", params={"stream": stream, "limit_worst": limit_worst})

    def get_gate_report(self, strategy_id: str) -> dict:
        """Composite read: container + lifecycle readiness + latest result.

        Mirrors the MCP forven_get_gate_report; use to diagnose why a strategy
        is/ isn't promotable without driving the lifecycle.
        """
        report: dict[str, Any] = {"strategy_id": strategy_id}
        try:
            report["container"] = self.get(f"/api/strategies/{strategy_id}/container")
        except ForvenAPIError as e:
            report["container_error"] = str(e)
        try:
            report["readiness"] = self.get(f"/api/lifecycle/strategies/{strategy_id}/readiness")
        except ForvenAPIError:
            report["readiness"] = None
        try:
            results = self.get("/api/results", params={"strategy": strategy_id, "limit": 1})
            rows = results.get("results") if isinstance(results, dict) else results
            if rows:
                report["latest_result"] = rows[0]
        except ForvenAPIError:
            pass
        return report

    def get_data_readiness(self, strategy_id: str | None = None, *, symbol: str | None = None,
                           timeframe: str | None = None, streams: Iterable[str] | None = None,
                           history_days: int | None = None, strategy_type: str | None = None,
                           code: str | None = None) -> Any:
        """The data contract: verdict ready / needs_data / blocked plus one row
        per requirement (candles, history, freshness, every feed) with a fix.

        Pass ``strategy_id`` for a registered strategy, or ``symbol`` +
        ``timeframe`` (+ ``streams`` like ``["funding", "oi"]``, ``code`` to scan
        a draft's source) for an idea. Check it before writing a strategy that
        reads anything beyond candles.
        """
        if strategy_id:
            return self.get(f"/api/data/readiness/strategy/{urllib.parse.quote(str(strategy_id), safe='')}")
        if not symbol or not timeframe:
            raise ValueError("pass strategy_id, or symbol and timeframe")
        body: dict[str, Any] = {"symbol": symbol, "timeframe": timeframe}
        for key, value in (("streams", list(streams) if streams else None), ("history_days", history_days),
                           ("strategy_type", strategy_type), ("code", code)):
            if value:
                body[key] = value
        return self.post("/api/data/readiness", body)

    def get_status(self, strategy_id: str) -> dict:
        """Lightweight {id, stage, status} for a strategy (for polling).

        The container nests these under `configuration` (with `strategy` and
        top-level as fallbacks across backend versions).
        """
        c = self.get_strategy(strategy_id)
        stage = status = None
        if isinstance(c, dict):
            for key in ("configuration", "strategy"):
                obj = c.get(key)
                if isinstance(obj, dict) and (obj.get("stage") or obj.get("status")):
                    stage, status = obj.get("stage"), obj.get("status")
                    break
            else:
                stage, status = c.get("stage"), c.get("status")
        return {"id": strategy_id, "stage": stage, "status": status}

    # ── write tools ────────────────────────────────────────────────────
    def create_session(self, label: str = "", actor: str = "ai-agent", objective: str = "") -> Any:
        return self.post("/api/ai-dropzone/sessions",
                         {"label": label, "actor": actor, "objective": objective})

    def close_session(self, session_id: str) -> Any:
        return self.post(f"/api/ai-dropzone/sessions/{session_id}/close")

    def register_file(self, file_path: str, session_id: str | None = None,
                      source: str = "ai_agent") -> Any:
        body: dict[str, Any] = {"file_path": file_path, "source": source}
        if session_id:
            body["session_id"] = session_id
        return self.post("/api/strategies/intake/register-file", body)

    def run_backtest(self, strategy_id: str, dataset_id: str, *, trade_mode: str | None = None,
                     parameters: dict | None = None, timeframe: str | None = None,
                     start: str | None = None, end: str | None = None,
                     leverage: float | None = None, session_id: str | None = None,
                     compact: bool = False) -> Any:
        body: dict[str, Any] = {"strategy_id": strategy_id, "dataset_id": dataset_id,
                                "request_source": "ai_agent"}
        for k, v in (("trade_mode", trade_mode), ("parameters", parameters), ("timeframe", timeframe),
                     ("start", start), ("end", end), ("leverage", leverage), ("session_id", session_id)):
            if v is not None:
                body[k] = v
        res = self.post("/api/backtesting/run", body)
        return self.compact_result(res) if compact else res

    def run_optimization(self, strategy_id: str, dataset_id: str, *, parameter_ranges: dict | None = None,
                         objective: str | None = None, n_trials: int | None = None, **kwargs: Any) -> Any:
        body: dict[str, Any] = {"strategy_id": strategy_id, "dataset_id": dataset_id}
        for k, v in (("parameter_ranges", parameter_ranges), ("objective", objective), ("n_trials", n_trials)):
            if v is not None:
                body[k] = v
        for k, v in kwargs.items():
            if v is not None:
                body[k] = v
        return self.post("/api/backtesting/optimize", body)

    def run_verdict(self, strategy_id: str, dataset_id: str, tests: list | None = None) -> Any:
        body: dict[str, Any] = {"strategy_id": strategy_id, "dataset_id": dataset_id}
        if tests:
            body["tests"] = tests
        return self.post("/api/backtesting/verdict/run", body)

    def promote(self, strategy_id: str, to_status: str, *, from_status: str | None = None,
                reason: str = "", force: bool = False) -> Any:
        body: dict[str, Any] = {"to_status": to_status, "reason": reason, "force": force}
        if from_status:
            body["from_status"] = from_status
        return self.post(f"/api/strategies/{strategy_id}/promote", body)

    # ── high-level helpers ─────────────────────────────────────────────
    @staticmethod
    def compact_result(result: Any) -> dict:
        """Reduce a backtest result to its in_sample/out_of_sample headline metrics."""
        if not isinstance(result, dict):
            return {"error": "non-dict result"}
        m = result.get("metrics") if isinstance(result.get("metrics"), dict) else result
        keys = ("profit_factor", "sharpe", "total_trades", "max_drawdown_pct",
                "win_rate", "total_return_pct")
        out: dict[str, Any] = {"result_id": result.get("result_id"),
                               "asset": result.get("asset"), "trade_mode": result.get("trade_mode")}
        for side in ("in_sample", "out_of_sample"):
            sub = m.get(side) if isinstance(m, dict) and isinstance(m.get(side), dict) else {}
            out[side] = {k: sub.get(k) for k in keys}
        return out

    @staticmethod
    def quick_screen(compact: dict, thresholds: dict | None = None) -> dict:
        """Pre-screen a compact backtest against the quick-screen gate (both windows).

        Returns {"pass": bool, "reasons": [..failing checks..]}.
        """
        t = {**QUICK_SCREEN_THRESHOLDS, **(thresholds or {})}
        reasons: list[str] = []

        def num(x):
            return x if isinstance(x, (int, float)) else None

        for side, min_tr in (("in_sample", t["min_trades_is"]), ("out_of_sample", t["min_trades_oos"])):
            s = compact.get(side, {}) if isinstance(compact, dict) else {}
            pf, sh, tr = num(s.get("profit_factor")), num(s.get("sharpe")), num(s.get("total_trades"))
            dd, ret = num(s.get("max_drawdown_pct")), num(s.get("total_return_pct"))
            if pf is None or pf < t["min_profit_factor"]:
                reasons.append(f"{side} profit_factor {pf} < {t['min_profit_factor']}")
            if sh is None or sh < t["min_sharpe"] or sh > t["max_sharpe"]:
                reasons.append(f"{side} sharpe {sh} out of [{t['min_sharpe']},{t['max_sharpe']}]")
            if dd is None or dd >= t["max_drawdown_pct"]:
                reasons.append(f"{side} max_drawdown_pct {dd} >= {t['max_drawdown_pct']}")
            if tr is None or tr < min_tr:
                reasons.append(f"{side} total_trades {tr} < {min_tr}")
            if ret is None or ret < t["min_total_return_pct"]:
                reasons.append(f"{side} total_return_pct {ret} < {t['min_total_return_pct']}")
        return {"pass": not reasons, "reasons": reasons}

    def enqueue_candidate(self, file_path: str, dataset_id: str, *, session_id: str | None = None,
                          thresholds: dict | None = None, trade_mode: str | None = None,
                          parameters: dict | None = None) -> dict:
        """Full genuine pipeline step: register -> backtest -> quick-screen -> promote to gauntlet.

        Never forces a gate (force=False). Returns a structured verdict; the
        background gauntlet Advancer then drives a genuine passer toward paper.
        """
        verdict: dict[str, Any] = {"file": file_path, "dataset_id": dataset_id}
        reg = self.register_file(file_path, session_id=session_id)
        sid = reg.get("strategy_id") if isinstance(reg, dict) else None
        verdict["strategy_id"] = sid
        verdict["lookahead_blocked"] = reg.get("lookahead_blocked") if isinstance(reg, dict) else None
        if not sid:
            verdict["error"] = "registration returned no strategy_id"
            return verdict
        res = self.run_backtest(sid, dataset_id, trade_mode=trade_mode, parameters=parameters,
                                session_id=session_id, compact=True)
        verdict["metrics"] = res
        screen = self.quick_screen(res, thresholds)
        verdict["quick_screen"] = screen
        if not screen["pass"]:
            verdict["enqueued"] = False
            return verdict
        promo = self.promote(sid, STAGE_GAUNTLET, from_status=STAGE_QUICK_SCREEN,
                             reason="ai_agent enqueue", force=False)
        msg = json.dumps(promo) if not isinstance(promo, str) else promo
        # A non-forced promotion races the Advancer; "found gauntlet" means it advanced.
        verdict["promotion"] = promo
        verdict["enqueued"] = bool(
            (isinstance(promo, dict) and promo.get("ok")) or ("found gauntlet" in msg))
        return verdict

    def wait_for_paper(self, strategy_ids: Iterable[str], *, timeout: float = 3600.0,
                       interval: float = 90.0) -> dict:
        """Poll until each strategy reaches paper or a terminal (archived/failed) state.

        Returns {id: {"stage", "status"}} final snapshot. HTTP-only (no DB).
        """
        ids = list(strategy_ids)
        deadline = time.time() + timeout
        last: dict[str, dict] = {}
        while time.time() < deadline:
            snap = {}
            for sid in ids:
                try:
                    snap[sid] = self.get_status(sid)
                except ForvenAPIError:
                    snap[sid] = {"id": sid, "status": "?"}
            last = snap
            done = all((s.get("status") in ("paper", "archived")) for s in snap.values())
            if done:
                break
            time.sleep(interval)
        return last
