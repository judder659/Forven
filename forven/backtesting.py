"""Forven backtesting client — connects Forven agents to the backtesting control plane.

By default the backtesting service runs on 127.0.0.1:8003, but this
is configurable with FORVEN_BACKTEST_API.
It exposes an AI Dropzone control plane for autonomous strategy
discovery, backtesting, optimization, and validation.

Key integration points:
- /api/backtesting/bootstrap — fetch config before first action
- /api/backtesting/run — start a backtest/run session
- /api/backtesting/runs — list past runs
- /api/backtesting/outcomes — get run outcomes
- /api/backtesting/status — get service status

Strategy operations (create, backtest, optimize, verdict) go through
the /backtesting/run endpoint with tool_name and arguments.
"""

import logging
import os
import time

import httpx

from forven.db import kv_get, kv_set

log = logging.getLogger("forven.backtesting")

# ── Configuration ────────────────────────────────────────────────────

_BACKTEST_API_ENV = "FORVEN_BACKTEST_API"
_DEFAULT_FORVEN_BASE = "http://127.0.0.1:8003"
_DEFAULT_FORVEN_API = f"{_DEFAULT_FORVEN_BASE}/api"


def _resolve_backtesting_api_base_url(base_url: str | None = None) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        for key in (
            _BACKTEST_API_ENV,
            "FORVEN_BACKTEST_API_URL",
            "FORVEN_BACKTESTING_API_URL",
            "FORVEN_BACKTEST_BASE_URL",
            "FORVEN_BACKTEST_BASE",
            "FORVEN_BACKTEST_REMOTE_API",
            "FORVEN_BACKTEST_RESULTS_REMOTE_API",
        ):
            candidate = str(os.getenv(key) or "").strip()
            if candidate:
                raw = candidate
                break
    if not raw:
        # Respect the active local API base first, so backtesting tools can
        # follow whichever backend port the stack is currently using.
        client_base = str(os.getenv("FORVEN_CLIENT_BASE") or "").strip()
        if client_base:
            raw = client_base
    if not raw:
        port = str(os.getenv("FORVEN_PORT") or "").strip()
        if port.isdigit():
            raw = f"http://127.0.0.1:{port}"
    if not raw:
        # Fall back to settings remote_engine_url when env var is unset.
        try:
            from forven.db import kv_get as _kv
            import json as _json
            settings_raw = _kv("settings")
            if settings_raw:
                settings = _json.loads(settings_raw) if isinstance(settings_raw, str) else settings_raw
                if settings.get("remote_engine_enabled") and settings.get("remote_engine_url"):
                    raw = str(settings["remote_engine_url"]).strip()
        except Exception:
            pass
    if not raw:
        raw = _DEFAULT_FORVEN_API
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    raw = raw.rstrip("/")
    if not raw.endswith("/api"):
        raw = f"{raw}/api"
    return raw


def _resolve_backtesting_service_url(api_base_url: str) -> str:
    normalized = str(api_base_url or "").rstrip("/")
    if normalized.endswith("/api"):
        return normalized[:-4]
    return normalized


FORVEN_API = _resolve_backtesting_api_base_url()
FORVEN_BASE = _resolve_backtesting_service_url(FORVEN_API)

# Cache bootstrap for 10 minutes
BOOTSTRAP_CACHE_TTL = 600


def _build_api_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    api_key = str(os.getenv("FORVEN_API_KEY") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    operator_key = str(os.getenv("FORVEN_OPERATOR_KEY") or "").strip()
    if operator_key:
        headers["x-operator-key"] = operator_key
    return headers


def _normalize_api_base(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    text = text.rstrip("/")
    if text.endswith("/api"):
        return text
    return f"{text}/api"


def _api_to_origin(api_base: str) -> str:
    base = str(api_base or "").rstrip("/")
    if base.endswith("/api"):
        return base[:-4]
    return base


def get_backtesting_api_base() -> str:
    """Resolve backtesting API base URL from env/settings with sane default."""
    for key in (
        "FORVEN_BACKTEST_API",
        "FORVEN_BACKTEST_API_URL",
        "FORVEN_BACKTESTING_API_URL",
        "FORVEN_BACKTEST_BASE_URL",
        "FORVEN_BACKTEST_BASE",
        "FORVEN_BACKTEST_REMOTE_API",
        "FORVEN_BACKTEST_RESULTS_REMOTE_API",
        "FORVEN_CLIENT_BASE",
    ):
        normalized = _normalize_api_base(os.environ.get(key))
        if normalized:
            return normalized

    port = str(os.environ.get("FORVEN_PORT", "")).strip()
    if port.isdigit():
        return _normalize_api_base(f"http://127.0.0.1:{port}") or _DEFAULT_FORVEN_API

    settings = kv_get("forven:settings", {})
    if isinstance(settings, dict):
        for key in (
            "backtesting_api_url",
            "backtesting_base_url",
            "backtesting_remote_api",
            "backtesting_remote_base",
        ):
            normalized = _normalize_api_base(settings.get(key))
            if normalized:
                return normalized

    return _DEFAULT_FORVEN_API


# ── Client ───────────────────────────────────────────────────────────

class BacktestingClient:
    """HTTP client for the backtesting AI Dropzone API.
    
    Uses direct /backtesting endpoints instead of session-based API.
    """

    def __init__(self, base_url: str | None = None, timeout: float = 300.0):
        resolved_base_url = _resolve_backtesting_api_base_url(base_url)
        self.base_url = resolved_base_url
        self.service_base_url = _resolve_backtesting_service_url(resolved_base_url)
        self.timeout = timeout
        self._client = httpx.Client(
            base_url=resolved_base_url,
            timeout=timeout,
            headers=_build_api_headers(),
        )

    def close(self):
        self._client.close()

    # ── Bootstrap & Discovery ────────────────────────────────────────

    def bootstrap(self) -> dict:
        """Fetch the bootstrap payload (cached in KV for 10 min)."""
        cached = kv_get("backtesting:bootstrap")
        if cached and time.time() - cached.get("_cached_at", 0) < BOOTSTRAP_CACHE_TTL:
            return cached

        resp = self._client.get("/backtesting/bootstrap")
        resp.raise_for_status()
        data = resp.json()
        data["_cached_at"] = time.time()
        kv_set("backtesting:bootstrap", data)
        return data

    def capabilities(self) -> dict:
        """Fetch machine-friendly capabilities doc."""
        resp = self._client.get("/backtesting/capabilities")
        resp.raise_for_status()
        return resp.json()

    def prompt_packs(self) -> dict:
        """Fetch available prompt packs."""
        resp = self._client.get("/backtesting/prompt-packs")
        resp.raise_for_status()
        return resp.json()

    def health(self) -> dict:
        """Check backtesting API health."""
        try:
            resp = httpx.get(f"{self.service_base_url}/health", timeout=5)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── Backtesting Operations ───────────────────────────────────────

    def list_datasets(self, symbol_filter: str = "", timeframe_filter: str = "") -> dict:
        """List available backtesting datasets."""
        resp = self._client.get("/backtesting/datasets", params={
            "symbol": symbol_filter,
            "timeframe": timeframe_filter,
        })
        # Handle 404 - return empty list if endpoint doesn't exist
        if resp.status_code == 404:
            return {"datasets": []}
        resp.raise_for_status()
        payload = resp.json()

        # Compatibility: some backend versions require exact symbol matches
        # (e.g. "BTC/USDT") and return empty for partial filters like "BTC".
        if not symbol_filter or not isinstance(payload, dict):
            return payload
        datasets = payload.get("datasets")
        if isinstance(datasets, list) and datasets:
            return payload

        fallback = self._client.get("/backtesting/datasets", params={
            "symbol": "",
            "timeframe": timeframe_filter,
        })
        if fallback.status_code == 404:
            return payload
        fallback.raise_for_status()
        fallback_payload = fallback.json()
        if not isinstance(fallback_payload, dict):
            return payload
        rows = fallback_payload.get("datasets")
        if not isinstance(rows, list):
            return payload

        needle = str(symbol_filter).strip().upper()
        if needle:
            rows = [
                row for row in rows
                if needle in str((row or {}).get("symbol", "")).upper()
            ]
        fallback_payload["datasets"] = rows
        return fallback_payload

    def create_strategy(
        self,
        name: str,
        type: str = "backtest",
        hypothesis_id: str | None = None,
        indicators: list = None,
        entry_conditions: list = None,
        exit_conditions: list = None,
        notes: str = "",
        filters: list = None,
        params: dict | None = None,
        symbol: str = "",
        timeframe: str = "1h",
    ) -> dict:
        """Create a new strategy."""
        payload = {
            "name": name,
            "type": type,
            "symbol": symbol,
            "timeframe": timeframe,
        }
        if hypothesis_id:
            payload["hypothesis_id"] = hypothesis_id
        if params is not None:
            payload["params"] = params
            if notes:
                payload["notes"] = notes
        else:
            # Backward compatibility: wrap rule-blob fields in params for the API.
            # This mirrors the fix in forven/routers/backtesting.py to support
            # certified strategy families (stochastic, williams_r, etc.) that
            # reject direct rule-blob fields.
            rule_fields = {
                "indicators": indicators or [],
                "entry_conditions": entry_conditions or [],
                "exit_conditions": exit_conditions or [],
                "notes": notes,
            }
            if filters:
                rule_fields["filters"] = filters
            payload["params"] = rule_fields
        resp = self._client.post("/backtesting/strategies", json=payload)
        resp.raise_for_status()
        return resp.json()

    def run_backtest(
        self,
        strategy_id: str,
        dataset_id: str,
        parameters: dict = None,
        fee_bps: float = 4.5,
        slippage_bps: float = 2.0,
        initial_capital: float | None = None,
        leverage: float | None = None,
        execution_controls: dict | None = None,
        objective: str = "sharpe_ratio",
        timeframe: str = "1h",
        trade_mode: str | None = None,
        request_source: str | None = None,
        origin_agent_id: str | None = None,
        origin_task_id: str | None = None,
    ) -> dict:
        """Run a backtest for a strategy on a dataset."""
        payload = {
            "strategy_id": strategy_id,
            "dataset_id": dataset_id,
            "fee_bps": fee_bps,
            "slippage_bps": slippage_bps,
            "objective": objective,
            "timeframe": timeframe,
        }
        if parameters:
            payload["parameters"] = parameters
        if initial_capital is not None:
            payload["initial_capital"] = initial_capital
        if leverage is not None:
            payload["leverage"] = leverage
        if isinstance(execution_controls, dict):
            for key, value in execution_controls.items():
                if value is not None:
                    payload[key] = value
        if trade_mode:
            payload["trade_mode"] = trade_mode
        if request_source:
            payload["request_source"] = request_source
        if origin_agent_id:
            payload["origin_agent_id"] = origin_agent_id
        if origin_task_id:
            payload["origin_task_id"] = origin_task_id
        
        resp = self._client.post("/backtesting/run", json=payload)
        resp.raise_for_status()
        return resp.json()

    def start_run(
        self,
        objective: str = "Discover profitable trading strategies",
        symbol_filter: str | None = None,
        timeframe_filter: str | None = None,
        prompt_pack: str = "explore",
        max_iterations: int = 50,
        **kwargs,
    ) -> dict:
        """Start a new AI-driven backtesting run (AI Dropzone session)."""
        payload = {
            "objective": objective,
            "symbol_filter": symbol_filter,
            "timeframe_filter": timeframe_filter,
            "prompt_pack": prompt_pack,
            "max_iterations": max_iterations,
        }
        payload.update(kwargs)
        
        resp = self._client.post("/backtesting/run", json=payload)
        resp.raise_for_status()
        return resp.json()

    def run_optimization(
        self,
        strategy_id: str,
        dataset_id: str,
        parameter_ranges: dict,
        objective: str = "sharpe_ratio",
        n_trials: int = 50,
    ) -> dict:
        """Run parameter optimization."""
        payload = {
            "strategy_id": strategy_id,
            "dataset_id": dataset_id,
            "parameter_ranges": parameter_ranges,
            "objective": objective,
            "n_trials": n_trials,
        }
        resp = self._client.post("/backtesting/optimize", json=payload)
        resp.raise_for_status()
        return resp.json()

    def run_verdict(
        self,
        strategy_id: str,
        dataset_id: str,
        tests: list = None,
    ) -> dict:
        """Run validation tests (walk-forward, monte carlo, etc.)."""
        payload = {
            "strategy_id": strategy_id,
            "dataset_id": dataset_id,
        }
        if tests:
            payload["tests"] = tests

        resp = self._client.post("/backtesting/verdict/run", json=payload)
        if resp.status_code == 404:
            resp = self._client.post("/verdict/run", json=payload)
        if resp.status_code == 404:
            resp = httpx.post(
                f"{self.service_base_url.rstrip('/')}/verdict/run",
                json=payload,
                timeout=self.timeout,
                headers=dict(self._client.headers),
            )
        resp.raise_for_status()
        return resp.json()

    def get_results(self, result_id: str, include_trades: bool = False, include_equity_curve: bool = False) -> dict:
        """Get backtest results."""
        params = {}
        if include_trades:
            params["include_trades"] = "true"
        if include_equity_curve:
            params["include_equity_curve"] = "true"
        resp = self._client.get(f"/backtesting/results/{result_id}", params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Legacy Compatibility ─────────────────────────────────────────

    def list_runs(self, limit: int = 20) -> list:
        """List past backtest runs (legacy compatibility)."""
        resp = self._client.get("/backtesting/runs", params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("runs", [])

    def get_outcomes(self) -> dict:
        """Get run outcomes (legacy compatibility)."""
        resp = self._client.get("/backtesting/outcomes")
        resp.raise_for_status()
        return resp.json()


# ── Singleton & Utilities ───────────────────────────────────────────

_client: BacktestingClient | None = None


def get_client() -> BacktestingClient:
    global _client
    resolved_base = _resolve_backtesting_api_base_url()
    if _client is None:
        _client = BacktestingClient(base_url=resolved_base)
    elif _client.base_url.rstrip("/") != resolved_base.rstrip("/"):
        try:
            _client.close()
        except Exception:
            pass
        _client = BacktestingClient(base_url=resolved_base)
    return _client


def is_available() -> bool:
    """Check if backtesting API is reachable."""
    try:
        result = get_client().health()
        return result.get("status") != "error"
    except Exception:
        return False
