import copy
import json
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
import time
from typing import Any

import httpx
from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect  # noqa: F401
from starlette.middleware.base import BaseHTTPMiddleware

from forven.ai import normalize_provider_and_model
from forven.codex_responses import is_openai_oauth_token
from forven.model_routing import (
    get_default_model_for_provider,
    get_model_routing_snapshot,
    update_model_routing,
)
# FORVEN_HOME lost its last in-file use when ARCH-06 step 4 moved
# _result_data_dirs to forven.backtest_api. Kept anyway: the shim contract for
# this module is "every name api_core exports today stays reachable here", and
# `api_core.FORVEN_HOME` has been a public attribute for the whole life of the
# file. Deleting it is a separate, grep-first decision.
from forven.config import AUTH_FILE, FORVEN_HOME, is_beta_build  # noqa: F401
from forven.agents.manager import create_agent, delete_agent, inspect_agent, update_agent
from forven.auth.store import (
    delete_profile,
    get_profile,
    get_token,
    is_profile_opaque,
    load_auth,
    upsert_profile,
)
from forven.auth.callback_listener import LoopbackCallbackListener
from forven.db import (
    auto_assign_best_symbol,
    build_strategy_container_name,
    create_pending_task,
    get_strategies,
    get_agents,
    get_db,
    kv_get,
    kv_set,
    kv_set_many,
    _now,
    log_activity,
    normalize_agent_visibility,
)
from forven.scheduler import (
    get_jobs,
    ensure_monitoring_jobs,
    migrate_data_engine_catchup_cadence,
    migrate_data_manager_jobs,
    migrate_legacy_scanner_cadence,
    reconcile_forven_jobs,
    seed_forven_jobs,
)
from forven.secret_storage import decrypt_secret, encrypt_secret
from forven import strategy_lifecycle as lifecycle_service
from forven.workspace import read_workspace, write_workspace
from forven.util import generate_pkce, generate_state, normalize_stage

# ARCH-06 step 1: the provider registry + model-discovery block (~900 lines)
# now lives in forven.providers.discovery. These re-exports are a COMPATIBILITY
# SHIM, not a convenience import — `forven.api_core` is the name every existing
# importer (and several tests, which reach for the private helpers by name)
# already binds to, so every symbol that used to be defined here is still
# reachable here. Do not prune them without grepping forven/, tests/ and
# scripts/ first.
from forven.providers import discovery as _model_discovery  # noqa: F401
from forven.providers.discovery import (  # noqa: F401
    _AGENT_MODEL_CATALOG,
    _AGENT_MODEL_LIST_CACHE,
    _AGENT_MODEL_LIST_CACHE_TTL_SECONDS,
    _AUTH_PROVIDER_ENV_VARS,
    _AUTH_TEST_ENDPOINT_OVERRIDES,
    _AUTH_TEST_HEADER_OVERRIDES,
    _DEFAULT_AGENT_MODEL_KEYS,
    _LOCAL_PROVIDER_DEFAULT_BASE_URLS,
    _MODEL_DISCOVERY_ALT_ENDPOINTS,
    _MODEL_DISCOVERY_HEADERS,
    _MODEL_PROVIDER_DISPLAY_NAMES,
    _SUPPORTED_AUTH_PROVIDERS,
    _ZAI_CANDIDATE_ENDPOINTS,
    _agent_model_option_key,
    _coerce_discovered_model_record,
    _collect_discovery_models,
    _default_agent_model_keys,
    _detect_zai_endpoint,
    _discovery_model_should_belong,
    _extract_discovery_models,
    _get_provider_base_url,
    _get_provider_discovery_token,
    _looks_like_anthropic_discovery_model,
    _looks_like_cerebras_discovery_model,
    _looks_like_deepseek_discovery_model,
    _looks_like_gemini_discovery_model,
    _looks_like_groq_discovery_model,
    _looks_like_lmstudio_discovery_model,
    _looks_like_minimax_discovery_model,
    _looks_like_mistral_discovery_model,
    _looks_like_nvidia_discovery_model,
    _looks_like_opencode_discovery_model,
    _looks_like_openai_discovery_model,
    _looks_like_xai_discovery_model,
    _looks_like_zai_discovery_model,
    _merge_model_records,
    _normalize_local_base_url,
    _normalize_model_id,
    _prettify_gemma_id,
    _provider_requires_token,
    _provider_supports_oauth,
)

# ARCH-06 step 2: the POST-body Pydantic models moved to forven.api_models.
# Same compatibility-shim rule as above — routers reference them as
# `core.XBody`, tests import them from `forven.api_core`, and evolution.py /
# gauntlet/tasks.py / phantom_recovery.py construct them directly.
from forven.api_models import (  # noqa: F401
    AgentDiscordTestBody,
    AuthProviderOAuthCompleteBody,
    AuthProviderOAuthStartBody,
    AuthProviderProfileBody,
    BacktestPreviewBody,
    BacktestSubmitBody,
    BacktestingRunBody,
    BrainChatBody,
    BrainChatHistoryEntry,
    ForceCloseTradeBody,
    LegacyAgentCreateBody,
    LegacyAgentDocumentBody,
    LegacyAgentModelBody,
    LegacyAgentUpdateBody,
    ManualStrategyBody,
    MarkTradeFailedBody,
    ModelPolicyUpdateBody,
    NlToSpecBody,
    OptimizationSubmitBody,
    PaperAdjustLevelBody,
    PaperAutoManagementBody,
    PaperClosePositionBody,
    PaperOpenPositionBody,
    PaperPartialCloseBody,
    PipelineSettingsUpdateBody,
    PreviewChartBody,
    SendToForgeBody,
    SettingsApiKeyBody,
    SettingsTestRemoteEngineBody,
)

# ARCH-06 step 3 (partial): the DECLARATIVE half of the settings subsystem —
# defaults, pure normalizers/coercers, the API-02 section schema + validator and
# the audit differ — moved to forven.settings_apply. The KV-touching half
# (_load/_save_settings_payload, the secrets/api-key/pipeline loaders and
# _apply_settings_section) deliberately stayed here; see the module docstring of
# forven.settings_apply for why moving it would silently detach the atomicity
# tests from the code they gate.
from forven.settings_apply import (  # noqa: F401
    DEFAULT_BACKTEST_DURATION_DAYS,
    _AUDIT_IGNORE_KEYS,
    _DEFAULT_API_KEY_SOURCES,
    _DEFAULT_GRAVEYARD_STRATEGY_LIMIT,
    _DEFAULT_PIPELINE_SETTINGS,
    _DEFAULT_SETTINGS_PAYLOAD,
    _GRAVEYARD_STRATEGY_STATUSES,
    _PIPELINE_STAGE_WIP_CAPS,
    _PIPELINE_WIP_CAP_UNLIMITED_VALUES,
    _SETTINGS_API_KEYS_STORAGE_KEY,
    _SETTINGS_MUTATION_LOCK,
    _SETTINGS_PIPELINE_STORAGE_KEY,
    _SETTINGS_SECRET_STORAGE_KEY,
    _SETTINGS_SECTION_KNOWN_KEYS,
    _SETTINGS_SECTION_NUMERIC_BOUNDS,
    _SETTINGS_STORAGE_KEY,
    _append_settings_audit,
    _coerce_agent_model_keys,
    _coerce_bool,
    _coerce_bounded_int,
    _coerce_float,
    _coerce_optional_int,
    _deep_merge_dicts,
    _default_data_engine_settings_payload,
    _default_pipeline_settings_payload,
    _default_research_settings_payload,
    _default_settings_payload,
    _diff_settings_section,
    _merge_data_engine_settings_payload,
    _merge_research_settings_payload,
    _normalize_agent_model_key,
    _normalize_graveyard_strategy_limit_mode,
    _normalize_graveyard_strategy_limit_payload,
    _normalize_graveyard_strategy_limit_value,
    _normalize_pipeline_wip_cap_mode,
    _normalize_pipeline_wip_cap_payload,
    _normalize_pipeline_wip_cap_value,
    _pipeline_wip_cap_kv_items,
    _validate_settings_section_payload,
)

# ARCH-06 step 4: backtest result PERSISTENCE + the API view-model normalizers
# (the row/trash writers, the on-disk artifact readers/writers, and the
# summary/detail/chart shapers) moved to forven.backtest_api. Same
# compatibility-shim rule as the three blocks above: forven/strategies/backtest.py,
# forven/robustness/engine.py, forven/evolution.py, forven/agents/tools_backtesting.py,
# forven/api_domains/{analytics,jobs}.py and ~10 test modules reach for these by
# name on `forven.api_core`.
#
# Those patches are also why this MUST stay a re-export rather than becoming a
# "just import it from the new module" cleanup: `_write_backtest_result_artifacts`,
# `_persist_backtest_result_row`, `_build_backtest_chart_context_payload`,
# `_result_data_dirs` and `_ensure_result_data_dir` are still api_core
# attributes, so monkeypatching them still swaps what the submission endpoints
# below actually call. The reverse direction — moved code reaching back for a
# patched name — goes through backtest_api's `core` proxy; see the seam rule in
# that module's docstring, and tests/test_finish_api_core.py which enforces it.
#
# NOT moved, deliberately: post_backtest_submit / post_optimization_submit /
# post_backtesting_run and _persist_completed_backtest_run. Those four are the
# money path and between them they touch SEVEN separately-patched api_core
# names (get_settings, _parse_strategy_params_blob, _require_existing_strategy_row,
# _infer_strategy_context_from_task_audit, _persist_backtest_result_row,
# _write_backtest_result_artifacts, _build_backtest_chart_context_payload).
# Moving them would trade one honest module boundary for seven proxy hops
# through the hottest code in the repo. They stay until those seams are
# rewritten to patch the owning module instead.
from forven.backtest_api import (  # noqa: F401
    _BACKTEST_DISPLAY_EQUITY,
    _backtest_trash_table,
    _build_backtest_chart_context_payload,
    _build_backtest_document,
    _build_file_only_backtest_detail,
    _build_sqlite_backtest_detail,
    _build_synthetic_equity_curve,
    _coerce_backtest_summary_payload,
    _coerce_iso_datetime,
    _coerce_legacy_metadata_float,
    _describe_strategy,
    _ensure_result_data_dir,
    _extract_result_type,
    _get_backtest_result_deleted_ids,
    _load_backtest_chart_artifact,
    _load_result_artifacts,
    _load_result_json_artifact,
    _normalize_backtest_chart_context_payload,
    _normalize_backtest_detail,
    _normalize_backtest_summary,
    _normalize_chart_bars,
    _normalize_chart_indicator_points,
    _normalize_chart_indicators,
    _normalize_chart_markers,
    _normalize_equity_points,
    _normalize_trade_artifact_rows,
    _normalize_trade_rows,
    _parse_json_blob,
    _persist_backtest_result_row,
    _record_backtest_sort_time,
    _result_artifact_candidate_ids,
    _result_data_dirs,
    _safe_result_artifact_key,
    _set_backtest_result_trash,
    _sqlite_backtest_summaries,
    _update_optimization_result_row,
    _write_backtest_chart_artifacts,
    _write_backtest_result_artifacts,
    calculate_backtest_verdict,
)


def _discover_provider_models(provider: str, force_refresh: bool = False) -> tuple[list[dict], str | None]:
    """Shim over forven.providers.discovery._discover_provider_models (ARCH-06).

    Not a plain re-export: the token getter is threaded through explicitly so
    that `monkeypatch.setattr(api_core, "_get_provider_discovery_token", ...)`
    still changes what discovery uses. Tests do exactly that to prove a ChatGPT
    OAuth token is never probed against api.openai.com/v1/models (it 401s), and
    a bare re-export would have silently stopped honouring the patch.
    """
    return _model_discovery._discover_provider_models(
        provider,
        force_refresh,
        token_getter=_get_provider_discovery_token,
    )

log = logging.getLogger("forven.api")
_BACKTEST_RESULTS_REMOTE_API_ENV = "FORVEN_BACKTEST_RESULTS_REMOTE_API"
_BACKTEST_RESULTS_REMOTE_TIMEOUT_SECONDS = 5.0
_LEGACY_API_SUNSET_HTTP = "Tue, 30 Jun 2026 00:00:00 GMT"


def json_safe_payload(value: Any) -> Any:
    """Return a payload FastAPI can serialize with strict JSON settings."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        sanitized: dict[Any, Any] = {
            key: json_safe_payload(item)
            for key, item in value.items()
        }
        profit_factor = value.get("profit_factor")
        if (
            isinstance(profit_factor, float)
            and math.isinf(profit_factor)
            and "profit_factor_is_infinite" not in sanitized
        ):
            sanitized["profit_factor_is_infinite"] = True
        return sanitized
    if isinstance(value, (list, tuple)):
        return [json_safe_payload(item) for item in value]
    return value


def _optimization_executor_workers() -> int:
    raw = str(os.environ.get("FORVEN_OPTIMIZATION_MAX_WORKERS", "2") or "").strip()
    try:
        parsed = int(raw)
    except Exception:
        parsed = 2
    return max(1, min(parsed, 4))


_OPTIMIZATION_EXECUTOR = ThreadPoolExecutor(
    max_workers=_optimization_executor_workers(),
    thread_name_prefix="opt",
)

# Slot tracking for optimization executor — reserve 1 slot for user work
_opt_system_running = 0
_opt_user_running = 0
_opt_lock = threading.Lock()
_OPT_USER_RESERVED_SLOTS = 1


class ForvenV1CompatMiddleware(BaseHTTPMiddleware):
	"""Allow frontend calls that still use `/api/forven/*`."""

	async def dispatch(self, request, call_next):
		path = request.scope.get("path", "")
		legacy_path = path if path.startswith("/api/forven") else None
		if path.startswith("/api/forven/"):
			request.scope["path"] = path.replace("/api/forven", "/api", 1)
		elif path == "/api/forven":
			request.scope["path"] = "/api"
		response = await call_next(request)
		if legacy_path:
			response.headers.setdefault("Deprecation", "true")
			response.headers.setdefault("Sunset", _LEGACY_API_SUNSET_HTTP)
			response.headers.setdefault("X-Forven-Legacy-Route", legacy_path)
		return response


_SCHEDULER_BOOTSTRAP_DONE = False
_SCHEDULER_BOOTSTRAP_LOCK = threading.Lock()


def _bootstrap_scheduler_jobs(force: bool = False):
    """Ensure scheduler table has expected defaults even when bot isn't running.

    Runs ONCE per process. get_scheduler() — an ops endpoint the dashboard polls
    — calls this on every request; without this guard each poll re-ran the full
    init_db() (all schema scripts + every migration) + the gauntlet backtest
    migration + job reconciliation, which a py-spy profile showed as steady-state
    CPU on the single API worker. The work is idempotent, so once per process is
    enough. Pass force=True to re-run intentionally (e.g. after a factory reset).
    """
    global _SCHEDULER_BOOTSTRAP_DONE
    if _SCHEDULER_BOOTSTRAP_DONE and not force:
        return
    with _SCHEDULER_BOOTSTRAP_LOCK:
        if _SCHEDULER_BOOTSTRAP_DONE and not force:
            return
        try:
            from forven.db import init_db

            init_db()
            # One-time gauntlet migration: demote strategies without canonical backtest
            try:
                from forven.brain import run_gauntlet_backtest_migration
                run_gauntlet_backtest_migration()
            except Exception as exc:
                log.warning("Gauntlet backtest migration failed: %s", exc)
            existing_jobs = get_jobs()
            if not existing_jobs:
                seed_forven_jobs()
                log.info("Seeded default scheduler jobs from API bootstrap")
            else:
                reconciliation = reconcile_forven_jobs()
                added_monitoring = ensure_monitoring_jobs()
                migrated_scanner = migrate_legacy_scanner_cadence()
                migrated_data_jobs = migrate_data_manager_jobs()
                migrated_catchup = migrate_data_engine_catchup_cadence()
                if reconciliation["removed"] or reconciliation["added"] or added_monitoring or migrated_data_jobs:
                    log.info(
                        "Scheduler reconciliation from API bootstrap: removed=%d added=%d monitoring_added=%d data_jobs_migrated=%d",
                        reconciliation["removed"],
                        reconciliation["added"],
                        added_monitoring,
                        migrated_data_jobs,
                    )
                elif migrated_scanner or migrated_catchup:
                    log.info(
                        "Applied scheduler legacy migration: scanner=%s catchup_cadence=%s",
                        migrated_scanner, migrated_catchup,
                    )
        except Exception as e:
            log.error("API scheduler bootstrap failed: %s", e)
            return
        _SCHEDULER_BOOTSTRAP_DONE = True


def mainnet_arming_snapshot() -> dict:
    """Whether REAL-MONEY order placement is armed on this process.

    OPS-4: ``FORVEN_ALLOW_MAINNET`` is the single switch between "every mainnet
    order is refused" and "orders spend real funds", and it was read exactly once
    deep inside forven.exchange.hyperliquid._assert_execution_allowed — never
    surfaced anywhere an operator looks. This is the read-only accessor every
    status surface should use.

    Delegates to the exchange module's ``mainnet_arming_state`` so there is ONE
    definition of armed; the env-var fallback only exists so a status endpoint
    can never be taken down by an exchange-module import error.
    """
    try:
        from forven.exchange.hyperliquid import mainnet_arming_state

        return dict(mainnet_arming_state())
    except Exception as exc:  # noqa: BLE001 — status surfaces must not hard-fail
        log.debug("mainnet_arming_state unavailable (%s); reading the env var", exc)
        armed = str(os.environ.get("FORVEN_ALLOW_MAINNET") or "").strip().lower() in {
            "1", "true", "yes", "on", "y",
        }
        return {
            "flag": "FORVEN_ALLOW_MAINNET",
            "armed": armed,
            "permits": (
                "REAL-MONEY orders on the Hyperliquid MAINNET endpoint"
                if armed
                else "testnet orders only — any mainnet-resolving order is refused"
            ),
            "source": "env_fallback",
        }


# API-08: the uvicorn request loop, captured once at startup. WebSocket
# connections are owned by THIS loop, so a threadpool handler (any sync `def`
# endpoint, any background thread) can never broadcast to them itself —
# `asyncio.run(ws_manager.broadcast(...))` spins up a brand-new loop, the send on
# a foreign-loop socket raises, and the old `except Exception: pass` around it
# swallowed the failure so the event just vanished. Cross into the real loop via
# call_soon_threadsafe, or skip and say so.
_API_EVENT_LOOP = None


def dispatch_ws_broadcast(message: dict) -> bool:
    """Schedule a ws_manager broadcast from ANY thread. True if it was queued.

    API-08. Safe to call from the request loop (schedules inline) or from a
    threadpool/background thread (hops to the captured API loop). When no API
    loop is running — tests, CLI entrypoints, a torn-down app — this logs and
    returns False rather than pretending the message was delivered.
    """
    import asyncio as _asyncio

    from forven.api_domains.live_ws import ws_manager

    try:
        running = _asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is not None and running.is_running():
        running.create_task(ws_manager.broadcast(message))
        return True

    loop = _API_EVENT_LOOP
    if loop is None or loop.is_closed() or not loop.is_running():
        log.debug(
            "No running API event loop — skipping WS broadcast %r",
            message.get("type"),
        )
        return False
    try:
        # Build the coroutine INSIDE the loop: if the loop dies between the
        # check above and the callback, no orphaned coroutine is left unawaited.
        loop.call_soon_threadsafe(lambda: loop.create_task(ws_manager.broadcast(message)))
        return True
    except RuntimeError as exc:
        log.warning("WS broadcast %r not delivered: %s", message.get("type"), exc)
        return False


async def _on_startup():
    import time as _time
    _BOOTSTRAP_MAX_RETRIES = 3
    _BOOTSTRAP_RETRY_DELAY = 5.0
    # API-08: capture the loop uvicorn serves requests on, so threadpool handlers
    # have somewhere legitimate to hand WS broadcasts.
    global _API_EVENT_LOOP
    try:
        import asyncio as _asyncio

        _API_EVENT_LOOP = _asyncio.get_running_loop()
    except RuntimeError:
        _API_EVENT_LOOP = None
    try:
        from forven.db import recover_dangling_runtime_tasks
        from forven.system_mode_policy import reconcile_manual_mode_backlog

        recovered = recover_dangling_runtime_tasks()
        if any(recovered.values()):
            log.info("Recovered dangling runtime tasks at API startup: %s", recovered)
        counts = reconcile_manual_mode_backlog()
        if counts.get("total"):
            log.info("Reconciled manual-mode backlog at API startup: %s", counts)
    except Exception as exc:
        log.warning("Startup queue reconciliation failed: %s", exc)
    try:
        seed_default_research_settings()
    except Exception as exc:
        log.warning("Research settings seeding failed: %s", exc)
    try:
        # Orphaned-job sweep belongs HERE, once per API boot — never at module
        # import, which spawn-context pool workers re-execute against the live
        # job table, failing genuinely-running jobs mid-flight (see
        # _cleanup_orphaned_running_jobs).
        from forven.routers.robustness import _cleanup_orphaned_running_jobs

        _cleanup_orphaned_running_jobs()
    except Exception as exc:
        log.warning("Orphaned running-job cleanup failed: %s", exc)
    try:
        from forven.data_manager import assert_data_root_consistent

        # Launch hardening: don't silently continue on a split-brain data root.
        # We do NOT hard-crash (the Tauri sidecar is supervised and would
        # crash-loop) — instead escalate to a prominent startup ERROR so the
        # operator sees that all enrichment/funding/OI data is unreliable until
        # FORVEN_DATA_DIR / FORVEN_HOME are aligned.
        if not assert_data_root_consistent():
            log.error(
                "DATA ROOT SPLIT-BRAIN at startup — strategies will enrich on "
                "empty funding/OI/macro (zeros) and ALL trading results are "
                "UNRELIABLE until FORVEN_DATA_DIR / FORVEN_HOME are aligned "
                "(exact paths in the warning above)."
            )
    except Exception as exc:
        log.warning("Data-root consistency check failed: %s", exc)
    for attempt in range(_BOOTSTRAP_MAX_RETRIES):
        try:
            _bootstrap_scheduler_jobs()
            return
        except Exception as exc:
            if attempt < _BOOTSTRAP_MAX_RETRIES - 1:
                log.warning(
                    "Scheduler bootstrap attempt %d/%d failed: %s — retrying in %.0fs",
                    attempt + 1, _BOOTSTRAP_MAX_RETRIES, exc, _BOOTSTRAP_RETRY_DELAY,
                )
                # TEST-9: the ONE sanctioned blocking sleep in this module. It
                # runs inside the startup hook, before uvicorn serves a single
                # request, so there is no request loop to starve — and the retry
                # must actually delay (an await here would let the app begin
                # serving with no scheduler jobs).
                #
                # This line WANTS a trailing per-line ASYNC251 suppression so the
                # whole-file exemption in pyproject.toml can be dropped. It does
                # not have one yet because the suppression and the pyproject entry
                # must land in the SAME commit as a third edit:
                # tests/test_harden_infra.py::test_test9_api_core_blocking_sleep_stays_a_single_known_instance
                # re-runs ruff with `lint.per-file-ignores = {}` and asserts
                # exactly ONE ASYNC251 hit here. A suppression removes that hit, so
                # adding it alone turns the pin test red. api_core.py is also now
                # clean under F841 (ARCH-07), so BOTH entries of the
                # "forven/api_core.py" per-file-ignores line can go at once.
                _time.sleep(_BOOTSTRAP_RETRY_DELAY)
            else:
                log.critical(
                    "Scheduler bootstrap FAILED after %d attempts: %s — API starting without scheduler jobs",
                    _BOOTSTRAP_MAX_RETRIES, exc,
                )
                try:
                    from forven.notifications import emit_notification
                    emit_notification(
                        "scheduler_bootstrap_failed",
                        severity="critical",
                        source="api_core",
                        title="CRITICAL: Scheduler bootstrap failed",
                        summary=f"Scheduler jobs could not be initialized after {_BOOTSTRAP_MAX_RETRIES} attempts. Last error: {exc}",
                        channel_name="alerts",
                        dedupe_key="scheduler_bootstrap_failed",
                    )
                except Exception:
                    pass


def _parse_bool_query(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "on", "y"}:
        return True
    if lowered in {"0", "false", "no", "off", "n"}:
        return False
    return default


def _parse_int_query(value: str | None, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(str(value).strip())
    except Exception:
        return default


_AUTH_OAUTH_SESSIONS: dict[str, dict[str, dict[str, object]]] = {}
_AUTH_OAUTH_CALLBACKS: dict[str, dict[str, str]] = {}
_AUTH_OAUTH_RESULTS: dict[str, dict[str, dict[str, object]]] = {}
_AUTH_OAUTH_SESSION_TTL_SECONDS = 15 * 60
_OPENAI_LOOPBACK_PORT = 1455
_OPENAI_OAUTH_LISTENER_TTL_SECONDS = 5 * 60


def _legacy_agent_model_options(force_refresh: bool = False) -> dict:
    enabled_models = set(_coerce_agent_model_keys(_load_settings_payload().get("agent_model_keys")))
    seen: set[str] = set()
    options: list[dict] = []
    provider_counts: dict[str, int] = {provider: 0 for provider in _SUPPORTED_AUTH_PROVIDERS}
    provider_errors: dict[str, str | None] = {provider: None for provider in _SUPPORTED_AUTH_PROVIDERS}
    provider_sources: dict[str, str] = {provider: "compat-fallback" for provider in _SUPPORTED_AUTH_PROVIDERS}

    for provider in _SUPPORTED_AUTH_PROVIDERS:
        discovered, discovery_error = _discover_provider_models(provider, force_refresh)
        provider_errors[provider] = discovery_error
        cache_entry = _AGENT_MODEL_LIST_CACHE.get(provider, {})
        provider_sources[provider] = str(cache_entry.get("source") or "compat-fallback")

        for model in discovered:
            provider_id = (model.get("provider") or provider).strip().lower()
            if provider_id != provider:
                continue
            model_id = str(model.get("model_id") or "").strip()
            if not model_id:
                continue

            model_key = f"{provider}:{model_id}"
            if model_key in seen:
                continue
            seen.add(model_key)
            provider_counts[provider] = provider_counts.get(provider, 0) + 1
            label = str(model.get("label") or "").strip() or model_id
            options.append(
                {
                    "key": model_key,
                    "provider": provider,
                    "model_id": model_id,
                    "label": label,
                    "enabled": model_key in enabled_models,
                },
            )

    for raw_key in enabled_models:
        if raw_key in seen:
            continue
        provider_raw, _, model_id = raw_key.partition(":")
        provider = provider_raw.strip().lower()
        if provider not in _SUPPORTED_AUTH_PROVIDERS or not model_id:
            continue
        resolved_key = _agent_model_option_key(provider, model_id)
        seen.add(resolved_key)
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
        options.append(
            {
                "key": resolved_key,
                "provider": provider,
                "model_id": model_id,
                "label": f"{_MODEL_PROVIDER_DISPLAY_NAMES.get(provider, provider.capitalize())} {model_id} (configured)",
                "enabled": True,
            },
        )

    providers = []
    for provider in _SUPPORTED_AUTH_PROVIDERS:
        providers.append(
            {
                "provider": provider,
                "default_model_id": get_default_model_for_provider(provider),
                "model_count": provider_counts.get(provider, 0),
                "source": provider_sources.get(provider) or "compat-fallback",
                "error": provider_errors.get(provider),
            },
        )
    return {
        "options": options,
        "providers": providers,
        "generated_at": _now(),
    }


def _coerce_expiry_ms(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except Exception:
        pass
    try:
        return int(datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return None


def _status_from_expiry(expires_ms: int | None) -> tuple[str, str | None]:
    now_ms = int(time.time() * 1000)
    if not expires_ms:
        return "active", None
    if now_ms >= expires_ms:
        return "expired", "Expired"
    if now_ms >= expires_ms - (5 * 60 * 1000):
        return "expiring_soon", "Expires soon"
    remaining = max(0, expires_ms - now_ms)
    days, rem = divmod(remaining, 86400000)
    if days:
        hours = rem // 3600000
        return "active", f"{days}d {hours}h remaining"
    hours, rem = divmod(remaining, 3600000)
    if hours:
        return "active", f"{hours}h remaining"
    minutes = rem // 60000
    return "active", f"{minutes}m remaining"


def _build_auth_provider_payload(provider: str) -> dict:
    profile = get_profile(provider) or {}
    token = str(profile.get("access") or profile.get("token") or profile.get("api_key") or "").strip()
    base_url = _get_provider_base_url(provider, profile)
    configured = bool(base_url) if provider == "lmstudio" else bool(token)

    # Distinguish "no profile on disk" from "profile on disk but ciphertext
    # can't be decrypted". The latter surfaces as `needs_reauth` so the UI
    # can prompt for re-entry without implying data was lost.
    needs_reauth = False
    if not configured:
        raw_profile = load_auth()["profiles"].get(f"{provider}:default")
        if is_profile_opaque(raw_profile):
            needs_reauth = True

    if configured:
        if provider == "lmstudio":
            status = "active"
            expires_in = None
            expires_at = None
        else:
            expires_ms = _coerce_expiry_ms(profile.get("expires"))
            status, expires_in = _status_from_expiry(expires_ms)
            expires_at = (
                datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                if expires_ms
                else None
            )
    elif needs_reauth:
        status = "needs_reauth"
        expires_in = None
        expires_at = None
    else:
        status = "not_configured"
        expires_in = None
        expires_at = None

    login_command = (
        "Configure LM Studio URL in Settings"
        if provider == "lmstudio"
        else f"export {_AUTH_PROVIDER_ENV_VARS[provider]}=<token>"
    )
    refresh_command = (
        "Local provider does not require refresh"
        if provider == "lmstudio"
        else f"forven auth refresh {provider}"
    )
    last_refresh_error = profile.get("last_refresh_error") if profile else None
    if last_refresh_error:
        status = "needs_reauth"

    payload = {
        "provider": provider,
        "configured": configured,
        "status": status,
        "expires_at": expires_at,
        "expires_in": expires_in,
        "has_refresh_token": bool(profile.get("refresh")),
        "login_command": login_command,
        "refresh_command": refresh_command,
        "supports_oauth": _provider_supports_oauth(provider),
        "requires_token": _provider_requires_token(provider),
        "base_url": base_url,
    }
    # "connected" = explicitly connected in-app (authorizes spend). Distinct from
    # merely "configured" (which a stray env-var key would also satisfy). This
    # MUST match the authoritative runtime callability gate exactly: membership
    # in the connected set AND a usable token. Otherwise an expired-token /
    # token-gone provider would show connected in the UI while the runtime
    # refuses to call it.
    try:
        from forven import model_selection

        payload["connected"] = model_selection.provider_is_connected(provider)
    except Exception:
        payload["connected"] = configured
    if last_refresh_error:
        payload["last_refresh_error"] = str(last_refresh_error)[:500]
    return payload


def _get_auth_providers_compat() -> dict:
    return {
        "providers": [
            _build_auth_provider_payload(provider)
            for provider in _SUPPORTED_AUTH_PROVIDERS
        ],
        "configure_command": "forven auth status",
        "status_command": "forven auth status",
        "auth_file": str(AUTH_FILE),
    }


def _get_model_policy_compat() -> dict:
    policy = get_model_routing_snapshot()
    configured_providers = [
        provider for provider in _SUPPORTED_AUTH_PROVIDERS
        if bool(_build_auth_provider_payload(provider)["configured"])
    ]
    policy_priority = [
        provider.lower()
        for provider in (policy.get("provider_priority") or [])
        if str(provider).strip().lower() in _SUPPORTED_AUTH_PROVIDERS
    ]
    provider_priority = [
        provider
        for provider in policy_priority
        if provider not in []  # dedupe below while preserving order
    ]
    seen_priority: set[str] = set()
    deduped_priority: list[str] = []
    for provider in provider_priority:
        if provider in seen_priority:
            continue
        seen_priority.add(provider)
        deduped_priority.append(provider)

    fallback_priority = [
        provider
        for provider in _SUPPORTED_AUTH_PROVIDERS
        if provider not in seen_priority
    ]
    fallback_priority = [provider for provider in (configured_providers + fallback_priority) if provider in _SUPPORTED_AUTH_PROVIDERS]
    provider_priority = deduped_priority + [provider for provider in fallback_priority if provider not in seen_priority]

    default_models = {
        provider: policy.get("default_models", {}).get(provider, get_default_model_for_provider(provider))
        for provider in _SUPPORTED_AUTH_PROVIDERS
        if provider in _SUPPORTED_AUTH_PROVIDERS
    }

    fallback_chains = {}
    for key, chain in (policy.get("fallback_chains") or {}).items():
        normalized = str(key).strip().lower()
        # Keep per-provider chains AND the slot-scoped chains the Routing &
        # Fallbacks UI writes (the global "backup" and "aux:<kind>"), so per-slot
        # fallback lists round-trip instead of being stripped on read.
        is_provider = normalized in _SUPPORTED_AUTH_PROVIDERS
        is_slot = (
            normalized == "backup"
            or normalized.startswith("aux:")
            or normalized.startswith("agent:")
        )
        if not (is_provider or is_slot):
            continue
        fallback_chains[normalized] = [
            {"provider": chain_entry.get("provider"), "model_id": chain_entry.get("model_id")}
            for chain_entry in chain
            if chain_entry.get("provider") and chain_entry.get("model_id")
        ]

    primary_provider = (provider_priority[0] if provider_priority else _SUPPORTED_AUTH_PROVIDERS[0])
    primary_model = str(default_models.get(primary_provider, "")).strip() or get_default_model_for_provider(primary_provider)
    return {
        "primary_provider": primary_provider,
        "primary_model": primary_model,
        "provider_priority": provider_priority,
        "default_models": {
            provider: str(model_id)
            for provider, model_id in default_models.items()
            if model_id
        },
        "fallback_chains": fallback_chains,
    }


def _not_connected_warning(provider: str, model: str) -> dict:
    """Structured warning for a (provider, model) whose provider is not connected."""
    return {
        "provider": provider,
        "model": model,
        "reason": "provider not connected — this selection will not run until you connect it",
    }


def _provider_is_connected_safe(provider: str) -> bool:
    """provider_is_connected() that fails open (True) so a model_selection import
    failure never invents spurious "not connected" warnings."""
    try:
        from forven import model_selection

        return model_selection.provider_is_connected(provider)
    except Exception:
        return True


def _collect_model_policy_warnings(next_policy: dict) -> list[dict]:
    """Warn for each (provider, model) the policy points at whose provider is not
    connected. Saving still proceeds (runtime fails closed anyway) — this is
    purely operator feedback so a selection that cannot run is visible."""
    warnings: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(provider: object, model: object) -> None:
        prov = str(provider or "").strip().lower()
        mdl = str(model or "").strip()
        if not prov:
            return
        key = (prov, mdl)
        if key in seen:
            return
        if _provider_is_connected_safe(prov):
            return
        seen.add(key)
        warnings.append(_not_connected_warning(prov, mdl))

    for provider, model in (next_policy.get("default_models") or {}).items():
        _add(provider, model)
    for chain in (next_policy.get("fallback_chains") or {}).values():
        for entry in chain or []:
            if isinstance(entry, dict):
                _add(entry.get("provider"), entry.get("model_id"))
    return warnings


def _coerce_model_policy_update_payload(body: "ModelPolicyUpdateBody") -> dict:
    updates = body.dict(exclude_unset=True)
    current = get_model_routing_snapshot()
    next_policy = {
        "provider_priority": updates.get("provider_priority", current.get("provider_priority", [])),
        "default_models": updates.get("default_models", current.get("default_models", {})),
        "fallback_chains": updates.get("fallback_chains", current.get("fallback_chains", {})),
        # Carry auxiliary forward so a model-policy save never silently resets it
        # to the hardcoded openrouter defaults (it is edited via
        # /api/brain/auxiliary). Without this, every routing save re-introduced
        # spend on an unconfigured provider.
        "auxiliary": updates.get("auxiliary", current.get("auxiliary", {})),
    }
    return update_model_routing(next_policy)


def _update_model_policy(body: "ModelPolicyUpdateBody") -> dict:
    saved = _coerce_model_policy_update_payload(body)
    response = _get_model_policy_compat()
    # Additive, backward-compatible: name each persisted (provider, model) whose
    # provider is not connected so the operator knows the selection will not run.
    response["warnings"] = _collect_model_policy_warnings(saved or {})
    return response


def _normalize_auth_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized not in _SUPPORTED_AUTH_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")
    return normalized


def _lookup_agent(agent_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM agents WHERE id = ?", (agent_id.strip(),)).fetchone()
        if not row:
            return None
        row_dict = dict(row)
    return _normalize_agent_model_row(row_dict)


def _normalize_agent_model_row(row: dict | None) -> dict | None:
    if not row:
        return None
    normalized = dict(row)
    normalized_model, normalized_model_id = normalize_provider_and_model(
        normalized.get("model"),
        normalized.get("model_id"),
    )
    normalized["model"] = normalized_model
    normalized["model_id"] = normalized_model_id
    normalized["visibility"] = normalize_agent_visibility(normalized.get("visibility"))
    normalized["has_discord_token"] = bool(normalized.get("discord_token"))
    normalized.pop("discord_token", None)
    return normalized


def _read_first_nonempty_workspace(paths: list[str]) -> str:
    """Return the first non-empty workspace file among ``paths`` (else "")."""
    for path in paths:
        content = read_workspace(path, optional=True)
        if content and content.strip():
            return content
    return ""


def _build_agent_documents(agent_id: str) -> dict:
    # SOUL.md and AGENTS.md are now PER-AGENT (agents/<id>/...), each seeded
    # from the shipped templates. Fall back to the GLOBAL file only when a
    # per-agent copy is absent (backward-compat for agents seeded before this
    # change). ROLE.md has always been per-agent.
    soul = _read_first_nonempty_workspace([
        f"agents/{agent_id}/SOUL.md",
        f"agents/{agent_id}/soul.md",
    ])
    if not soul:
        soul = read_workspace("SOUL.md", optional=True) or ""

    agents = _read_first_nonempty_workspace([
        f"agents/{agent_id}/AGENTS.md",
        f"agents/{agent_id}/agents.md",
    ])
    if not agents:
        agents = read_workspace("AGENTS.md", optional=True) or ""

    role = _read_first_nonempty_workspace([
        f"agents/{agent_id}/ROLE.md",
        f"agents/{agent_id}/role.md",
    ])
    if not role:
        db_agent = _lookup_agent(agent_id)
        role = str((db_agent or {}).get("role", ""))
    return {"soul": soul, "agents": agents, "role": role}


def _inject_agent_role_from_workspace(agent_row: dict | None) -> dict | None:
    """Attach workspace ROLE.md content onto agent rows (if available)."""
    if not agent_row:
        return agent_row

    normalized = _normalize_agent_model_row(dict(agent_row)) or {}
    documents = _build_agent_documents(str(normalized.get("id", "")))
    if documents.get("role"):
        normalized["role"] = documents["role"]
    normalized["has_role_md"] = bool(documents.get("role"))
    return normalized


def _safe_json(value):
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _to_datetime_sort_key(value) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            try:
                return float(value)
            except Exception:
                return 0.0


def _parse_timestamp(value: object) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_exchange_testnet() -> bool:
    from forven.api_domains.trading import _resolve_exchange_testnet as _domain_resolve_exchange_testnet

    return bool(_domain_resolve_exchange_testnet())


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except Exception:
            return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        return float(raw)
    except Exception:
        return None


def is_graveyard_strategy_status(status: str | None) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized in _GRAVEYARD_STRATEGY_STATUSES


def configured_graveyard_strategy_limit() -> int | None:
    payload = _load_pipeline_settings_payload()
    mode = _normalize_graveyard_strategy_limit_mode(payload.get("graveyard_strategy_limit_mode"))
    if mode == "unlimited":
        return None
    return _normalize_graveyard_strategy_limit_value(
        payload.get("graveyard_strategy_limit"),
        _DEFAULT_GRAVEYARD_STRATEGY_LIMIT,
    )


def resolve_strategy_query_limit(status: str | None, requested_limit: object = None, offset: object = 0) -> int | None:
    """Resolve API strategy list limits, honoring the configurable graveyard cap."""
    try:
        bounded_offset = max(0, int(offset or 0))
    except Exception:
        bounded_offset = 0

    graveyard_limit = configured_graveyard_strategy_limit() if is_graveyard_strategy_status(status) else None
    if graveyard_limit is not None:
        remaining = graveyard_limit - bounded_offset
        if remaining <= 0:
            return 0
    else:
        remaining = None

    try:
        parsed_requested = None if requested_limit is None else int(requested_limit)
    except Exception:
        parsed_requested = None

    if is_graveyard_strategy_status(status) and graveyard_limit is None:
        if parsed_requested is None or parsed_requested <= 0:
            return None
        return max(1, min(parsed_requested, 1000))

    if parsed_requested is None or parsed_requested <= 0:
        parsed_requested = graveyard_limit or 500

    bounded_limit = max(1, min(parsed_requested, 1000))
    if remaining is not None:
        return min(bounded_limit, remaining)
    return bounded_limit


def _sync_pipeline_wip_cap_kv(payload: dict) -> None:
    kv_set_many(_pipeline_wip_cap_kv_items(payload))


def _has_open_book_routed_trades() -> bool:
    """True if any OPEN live trade is routed to a direction sub-account.

    Used to refuse re-pointing/clearing a book address (or disabling books)
    while a position lives in that book — otherwise the eventual CLOSE would
    route to the wrong account and silently no-op, leaving a live position open.
    """
    try:
        from forven.db import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM trades WHERE status = 'OPEN' AND book IS NOT NULL "
                "AND book != '' AND book != 'main' LIMIT 1"
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _load_settings_secrets() -> dict:
    raw = kv_get(_SETTINGS_SECRET_STORAGE_KEY, {})
    if not isinstance(raw, dict):
        return {}
    secrets: dict = {}
    for key, value in raw.items():
        if isinstance(value, str) and value:
            secrets[key] = decrypt_secret(value)
        else:
            secrets[key] = value
    return secrets


def _encrypt_settings_secrets(payload: dict) -> dict:
    """Encrypt a plaintext secrets dict into its at-rest KV form (no write)."""
    encrypted: dict = {}
    for key, value in payload.items():
        if isinstance(value, str):
            encrypted[key] = encrypt_secret(value.strip()) if value.strip() else ""
        else:
            encrypted[key] = value
    return encrypted


def _save_settings_secrets(payload: dict) -> None:
    kv_set(_SETTINGS_SECRET_STORAGE_KEY, _encrypt_settings_secrets(payload))


def _load_settings_payload() -> dict:
    raw = kv_get(_SETTINGS_STORAGE_KEY, {})
    payload = _default_settings_payload()
    if isinstance(raw, dict):
        payload.update(raw)

    payload["research_settings"] = _merge_research_settings_payload(payload.get("research_settings"))
    payload["data_engine_settings"] = _merge_data_engine_settings_payload(payload.get("data_engine_settings"))

    payload["hyperliquid_wallet"] = str(payload.get("hyperliquid_wallet") or "").strip()
    payload["hyperliquid_api_address"] = str(payload.get("hyperliquid_api_address") or "").strip()

    # Hyperliquid is the only supported live-execution venue. Normalize any
    # legacy/removed selection (e.g. a stored "binance") so the UI and runtime
    # always see a valid executable exchange.
    if str(payload.get("exchange") or "").strip().lower() != "hyperliquid":
        payload["exchange"] = "hyperliquid"

    secrets = _load_settings_secrets()
    payload["agent_model_keys"] = _coerce_agent_model_keys(payload.get("agent_model_keys"))
    payload["hyperliquid_has_key"] = bool(str(secrets.get("hyperliquid_private_key", "")).strip())
    payload["discord_webhook_configured"] = bool(str(secrets.get("discord_webhook_url", "")).strip())
    # Check if main bot token is configured in config.json or DISCORD_TOKEN env var
    try:
        import os as _os
        from forven.config import load_config
        cfg = load_config()
        has_config_token = bool(str(cfg.get("discord_token", "")).strip())
        has_env_token = bool(str(_os.environ.get("DISCORD_TOKEN", "")).strip())
        payload["discord_bot_token_configured"] = has_config_token or has_env_token
        payload["discord_bot_token_source"] = "config" if has_config_token else ("env" if has_env_token else "none")
    except Exception:
        payload["discord_bot_token_configured"] = False
        payload["discord_bot_token_source"] = "none"
    payload["updated_at"] = str(payload.get("updated_at") or _now())
    return payload


def _save_settings_payload(payload: dict) -> None:
    kv_set(_SETTINGS_STORAGE_KEY, payload)


def seed_default_research_settings() -> dict:
    raw = kv_get(_SETTINGS_STORAGE_KEY, {})
    payload = _load_settings_payload()
    normalized_research_settings = _merge_research_settings_payload(
        raw.get("research_settings") if isinstance(raw, dict) else None
    )
    should_persist = not isinstance(raw, dict) or raw.get("research_settings") != normalized_research_settings
    if should_persist:
        payload["research_settings"] = normalized_research_settings
        _save_settings_payload(payload)
    return payload


def _load_api_keys_payload() -> dict:
    raw = kv_get(_SETTINGS_API_KEYS_STORAGE_KEY, {})
    if isinstance(raw, dict):
        payload: dict = {}
        for source, entry in raw.items():
            if isinstance(entry, dict):
                record = dict(entry)
                value = record.get("value")
                if isinstance(value, str) and value:
                    record["value"] = decrypt_secret(value)
                payload[source] = record
            elif isinstance(entry, str):
                payload[source] = decrypt_secret(entry)
            else:
                payload[source] = entry
        return payload
    return {}


def _save_api_keys_payload(payload: dict) -> None:
    encrypted: dict = {}
    for source, entry in payload.items():
        if isinstance(entry, dict):
            record = dict(entry)
            value = str(record.get("value") or "").strip()
            record["value"] = encrypt_secret(value) if value else ""
            encrypted[source] = record
        else:
            value = str(entry or "").strip()
            encrypted[source] = encrypt_secret(value) if value else ""
    kv_set(_SETTINGS_API_KEYS_STORAGE_KEY, encrypted)


def _normalize_api_key_source(source: str) -> str:
    return str(source or "").strip().lower().replace(" ", "-")


def _load_pipeline_settings_payload() -> dict:
    raw = kv_get(_SETTINGS_PIPELINE_STORAGE_KEY, {})
    payload = _default_pipeline_settings_payload()
    if isinstance(raw, dict):
        payload.update(raw)
    _normalize_pipeline_wip_cap_payload(payload)
    _normalize_graveyard_strategy_limit_payload(payload)
    payload["created_by"] = str(payload.get("created_by") or "system")
    payload["created_at"] = str(payload.get("created_at") or _now())
    return payload


def _save_pipeline_settings_payload(payload: dict) -> None:
    kv_set(_SETTINGS_PIPELINE_STORAGE_KEY, payload)


# Maps each Notifications-panel toggle to the notification_preferences keys it
# drives. Used for BOTH the write bridge (_apply_settings_section 'notifications')
# and the get_settings read-back so the round-trip is consistent: one toggle sets
# all N prefs on write; on read a toggle is "on" only if every pref it drives is on.
_NOTIF_TOGGLE_PREF_KEYS: dict[str, tuple[str, ...]] = {
    "notify_on_entry": ("trade_opened_to_discord",),
    "notify_on_exit": ("trade_closed_to_discord",),
    "notify_daily_summary": ("digests_to_discord",),
    "notify_health_reports": ("system_degraded_to_discord", "system_recovered_to_discord"),
    "notify_errors": ("trade_failed_to_discord", "agent_failure_to_discord", "risk_critical_to_discord"),
}


def _apply_settings_section(section: str, payload: dict, actor: str = "ui") -> dict:
    """Apply a settings-section edit and persist it atomically.

    The whole load -> mutate -> diff -> audit -> persist sequence must run
    under ``_SETTINGS_MUTATION_LOCK`` (the endpoint acquires it). Within one
    call every touched KV key — the encrypted secrets blob and the main
    settings blob, audit entry included — is written in a SINGLE transaction
    via ``kv_set_many`` so a crash can never split them. The audit diff is
    computed here (against the ``old`` snapshot taken before mutation) and
    attributed to ``actor``, so with the lock held each entry reflects exactly
    its own request's changes.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="settings payload must be an object")

    updates = _load_settings_payload()
    secrets = _load_settings_secrets()
    # Snapshot the pre-mutation blob for the audit diff. `updates` is mutated in
    # place below, so a shallow copy would alias nested dicts and diff to
    # nothing; deep-copy to capture the true "from" values.
    old_snapshot = copy.deepcopy(updates)

    section = str(section or "").strip().lower()
    if section in {"pipeline", "api-keys", "test-discord", "reset"}:
        raise HTTPException(status_code=404, detail=f"settings section not supported: {section}")

    # API-02: validate BEFORE the first mutation so a rejected write leaves the
    # blob (and therefore live enforcement) exactly as it was.
    _validate_settings_section_payload(section, payload)

    if section == "exchange":
        exchange = str(payload.get("exchange", "")).strip().lower()
        if exchange:
            # Hyperliquid is the only supported live-execution venue; coerce any
            # other value (e.g. a legacy "binance" selection) to hyperliquid.
            updates["exchange"] = exchange if exchange == "hyperliquid" else "hyperliquid"

    elif section == "hyperliquid":
        wallet_payload_key = None
        for candidate in ("actual_wallet_address", "wallet_address", "hyperliquid_wallet"):
            if candidate in payload:
                wallet_payload_key = candidate
                break
        if wallet_payload_key:
            updates["hyperliquid_wallet"] = str(payload.get(wallet_payload_key) or "").strip()

        api_address_payload_key = None
        for candidate in ("api_address", "hyperliquid_api_address"):
            if candidate in payload:
                api_address_payload_key = candidate
                break
        if api_address_payload_key:
            updates["hyperliquid_api_address"] = str(payload.get(api_address_payload_key) or "").strip()

        private_key_payload_key = None
        for candidate in ("api_secret_key", "private_key", "hyperliquid_private_key"):
            if candidate in payload:
                private_key_payload_key = candidate
                break
        if private_key_payload_key:
            private_key = str(payload.get(private_key_payload_key) or "").strip()
            if private_key:
                # Normalize to a 0x-prefixed key at the storage boundary so every
                # later read is 0x-anchored and the log redactor (which matches
                # 0x + 64 hex) always catches the funds-controlling key if it ever
                # leaks. A bare 64-hex key would slip past the redactor (audit P1.5).
                private_key = private_key.strip("'\"").strip()
                if private_key and not private_key.lower().startswith("0x"):
                    private_key = "0x" + private_key
                secrets["hyperliquid_private_key"] = private_key
                # Only auto-derive if the payload and existing settings do not already pin an API address.
                if not api_address_payload_key and not str(updates.get("hyperliquid_api_address") or "").strip():
                    try:
                        from eth_account import Account as _EthAccount
                        updates["hyperliquid_api_address"] = str(_EthAccount.from_key(private_key).address)
                    except Exception:
                        pass
            else:
                secrets.pop("hyperliquid_private_key", None)
        testnet_payload_key = None
        for candidate in ("use_testnet", "hyperliquid_testnet"):
            if candidate in payload:
                testnet_payload_key = candidate
                break
        if testnet_payload_key:
            updates["hyperliquid_testnet"] = _coerce_bool(
                payload.get(testnet_payload_key),
                updates["hyperliquid_testnet"],
            )

    elif section == "trading-mode":
        if "trading_mode" in payload:
            requested = str(payload.get("trading_mode") or updates["trading_mode"]).strip().lower()
            # Beta builds are hard-locked to paper. Silently coerce rather
            # than 400-ing so a settings write that happens to carry an
            # unrelated field doesn't blow up, but log so it's auditable.
            if requested == "live" and is_beta_build():
                log.warning("refusing trading_mode=live in beta build; coercing to paper")
                requested = "paper"
            # CFG-1: the visible 'Trading mode' select must actually arm/disarm the
            # engine. The execution path reads config.get_execution_mode()
            # (config.json execution_mode), NOT this KV key, so without this the
            # control was a no-op and the dashboard could misreport live vs paper.
            # MODE-SPLIT-1: enforcement is the gatekeeper — persist this KV
            # mirror only AFTER set_execution_mode accepts. Previously a
            # refused 'live' was stored anyway, so Settings displayed live
            # while the runtime stayed paper. A refusal is a loud 400 the
            # save bar surfaces, never a silently-divergent stored value.
            try:
                from forven.config import set_execution_mode
                set_execution_mode(requested)
            except Exception as exc:
                log.warning("refusing to persist trading_mode=%s: %s", requested, exc)
                raise HTTPException(
                    status_code=400,
                    detail=f"trading_mode '{requested}' was not applied: {exc}",
                ) from exc
            updates["trading_mode"] = requested

    elif section == "initial-capital":
        if "initial_capital" in payload:
            updates["initial_capital"] = _coerce_float(payload.get("initial_capital"), updates["initial_capital"])

    elif section == "risk":
        # Per-trade risk twins. Enforcement (exchange/risk._get_risk_limits)
        # prefers max_risk_per_trade_pct and only falls back to the legacy
        # max_position_size_pct when the preferred key is absent — and the
        # preferred key is ALWAYS seeded in this blob. Writing either key
        # therefore syncs BOTH (unless the payload sets each explicitly), so no
        # write path is a placebo and every reader sees the same limit.
        if "max_risk_per_trade_pct" in payload:
            risk_pct = _coerce_float(
                payload.get("max_risk_per_trade_pct"),
                _coerce_float(updates.get("max_risk_per_trade_pct"), 10.0),
            )
            updates["max_risk_per_trade_pct"] = risk_pct
            if "max_position_size_pct" not in payload:
                updates["max_position_size_pct"] = risk_pct
        if "max_position_size_pct" in payload:
            position_pct = _coerce_float(payload.get("max_position_size_pct"), updates["max_position_size_pct"])
            updates["max_position_size_pct"] = position_pct
            if "max_risk_per_trade_pct" not in payload:
                updates["max_risk_per_trade_pct"] = position_pct
        # Daily-loss twins. Enforcement uses max_daily_loss_pct whenever it is
        # present (always, since it is seeded) and only derives from the legacy
        # USD max_daily_loss when the pct twin is missing. Keep both coherent
        # against initial_capital on every write.
        if "max_daily_loss_pct" in payload:
            daily_pct = _coerce_float(
                payload.get("max_daily_loss_pct"),
                _coerce_float(updates.get("max_daily_loss_pct"), 2.0),
            )
            updates["max_daily_loss_pct"] = daily_pct
            capital = _coerce_float(updates.get("initial_capital"), 0.0)
            if "max_daily_loss" not in payload and capital > 0:
                updates["max_daily_loss"] = round(capital * daily_pct / 100.0, 2)
        if "max_daily_loss" in payload:
            daily_usd = _coerce_float(payload.get("max_daily_loss"), updates["max_daily_loss"])
            updates["max_daily_loss"] = daily_usd
            capital = _coerce_float(updates.get("initial_capital"), 0.0)
            if "max_daily_loss_pct" not in payload and capital > 0:
                updates["max_daily_loss_pct"] = round(daily_usd / capital * 100.0, 4)
        if "max_drawdown_pct" in payload:
            updates["max_drawdown_pct"] = _coerce_float(payload.get("max_drawdown_pct"), updates["max_drawdown_pct"])
        if "max_concurrent_positions" in payload:
            updates["max_concurrent_positions"] = _coerce_optional_int(payload.get("max_concurrent_positions"), updates["max_concurrent_positions"])
        if "paper_max_concurrent_positions" in payload:
            updates["paper_max_concurrent_positions"] = _coerce_optional_int(
                payload.get("paper_max_concurrent_positions"),
                updates.get("paper_max_concurrent_positions", 0),
            )
        # Guard book-routing changes while positions are open in those books:
        # re-pointing/clearing an address (or disabling books) would mis-route
        # the eventual CLOSE. Computed once, lazily.
        _book_routing_locked = _has_open_book_routed_trades()
        if "live_books_enabled" in payload:
            _new_enabled = _coerce_bool(
                payload.get("live_books_enabled"), bool(updates.get("live_books_enabled", False))
            )
            if _new_enabled is False and bool(updates.get("live_books_enabled", False)) and _book_routing_locked:
                log.warning("Refusing to disable live direction books while book-routed live positions are open; keeping enabled.")
            else:
                updates["live_books_enabled"] = _new_enabled
        for _book_key in ("hyperliquid_long_book_address", "hyperliquid_short_book_address"):
            if _book_key in payload:
                _new_addr = str(payload.get(_book_key) or "").strip()
                _cur_addr = str(updates.get(_book_key) or "").strip()
                if _new_addr != _cur_addr and _book_routing_locked:
                    log.warning(
                        "Refusing to change %s while book-routed live positions are open; keeping %s.",
                        _book_key, _cur_addr or "(blank)",
                    )
                else:
                    updates[_book_key] = _new_addr
        if "hyperliquid_use_cross_margin" in payload:
            updates["hyperliquid_use_cross_margin"] = _coerce_bool(
                payload.get("hyperliquid_use_cross_margin"),
                bool(updates.get("hyperliquid_use_cross_margin", False)),
            )
        if "liq_distance_warn_pct" in payload:
            updates["liq_distance_warn_pct"] = _coerce_float(payload.get("liq_distance_warn_pct"), updates.get("liq_distance_warn_pct", 15))
        if "liq_distance_critical_pct" in payload:
            updates["liq_distance_critical_pct"] = _coerce_float(payload.get("liq_distance_critical_pct"), updates.get("liq_distance_critical_pct", 7))
        if "cooldown_after_loss_hours" in payload:
            updates["cooldown_after_loss_hours"] = _coerce_float(payload.get("cooldown_after_loss_hours"), updates["cooldown_after_loss_hours"])
        # PORT-1: live account-level portfolio budget (read top-level from
        # forven:settings by forven.exchange.risk.check_live_portfolio_budget).
        if "live_portfolio_budget_enabled" in payload:
            updates["live_portfolio_budget_enabled"] = _coerce_bool(
                payload.get("live_portfolio_budget_enabled"),
                bool(updates.get("live_portfolio_budget_enabled", True)),
            )
        for _pb_key, _pb_default in (
            ("live_max_total_open_risk_pct", 5.0),
            ("live_max_asset_exposure_pct", 150.0),
            ("live_max_group_exposure_pct", 200.0),
            # SIZE-CAP-1: per-order hard ceilings (forven.exchange.risk).
            ("live_hard_max_per_trade_risk_pct", 2.0),
            ("live_hard_max_order_notional_pct", 100.0),
            # BOOK-BUDGET-1: per-wallet gross-notional cap (forven.exchange.risk).
            ("live_max_book_notional_pct", 100.0),
            # CORR-1: measured-correlation effective-exposure gate
            # (forven.portfolio_correlation via check_live_portfolio_budget).
            ("live_max_effective_exposure_pct", 200.0),
            ("live_correlation_window_bars", 720.0),
            ("live_correlation_missing_default", 1.0),
            # RETRY-STORM-1: failed-open retry brake (forven.exchange.risk.can_open).
            ("live_failed_open_cooldown_minutes", 15.0),
            ("live_failed_open_max_attempts", 3.0),
            ("live_failed_open_window_hours", 6.0),
            # PORT-LAYER-1: portfolio allocator (forven.portfolio_allocator).
            ("portfolio_lookback_days", 60.0),
            ("portfolio_target_book_vol_pct", 0.0),
            ("portfolio_min_risk_multiplier", 0.25),
            ("portfolio_max_risk_multiplier", 2.0),
            # PORT-LAYER-2: funding-carry basket (forven.basket_runtime).
            ("basket_rebalance_hours", 24.0),
            ("basket_n_legs", 5.0),
            ("basket_gross_leverage", 1.0),
            ("basket_universe_min_bars", 17520.0),
            # BASKET-2: incumbency buffer on basket re-ranking (forven.basket_runtime).
            ("basket_rank_buffer", 3.0),
            # LIVE-LOOP-1: paper→live graduation recommender (forven.live_graduation).
            ("graduation_min_soak_days", 14.0),
            ("graduation_min_paper_trades", 10.0),
            ("graduation_min_measured_trades", 5.0),
            ("graduation_base_arm_usd", 100.0),
            ("graduation_max_arm_usd", 250.0),
            ("graduation_daily_limit", 2.0),
            ("graduation_deny_cooldown_days", 7.0),
            ("graduation_skew_lookback_days", 30.0),
        ):
            if _pb_key in payload:
                updates[_pb_key] = _coerce_float(payload.get(_pb_key), _coerce_float(updates.get(_pb_key), _pb_default))
        if "live_correlation_budget_enabled" in payload:
            updates["live_correlation_budget_enabled"] = _coerce_bool(
                payload.get("live_correlation_budget_enabled"),
                bool(updates.get("live_correlation_budget_enabled", True)),
            )
        # PORT-LAYER-1 toggles (forven.portfolio_allocator).
        if "portfolio_allocator_enabled" in payload:
            updates["portfolio_allocator_enabled"] = _coerce_bool(
                payload.get("portfolio_allocator_enabled"),
                bool(updates.get("portfolio_allocator_enabled", False)),
            )
        if "portfolio_allocator_live" in payload:
            updates["portfolio_allocator_live"] = _coerce_bool(
                payload.get("portfolio_allocator_live"),
                bool(updates.get("portfolio_allocator_live", False)),
            )
        # PORT-GATE-1: master switch for the whole portfolio layer.
        if "portfolio_layer_enabled" in payload:
            updates["portfolio_layer_enabled"] = _coerce_bool(
                payload.get("portfolio_layer_enabled"),
                bool(updates.get("portfolio_layer_enabled", False)),
            )
        # LIVE-LOOP-1 toggle: graduation recommender ships dark
        # (forven.live_graduation — recommendation-only, never arms capital).
        if "live_graduation_recommender_enabled" in payload:
            updates["live_graduation_recommender_enabled"] = _coerce_bool(
                payload.get("live_graduation_recommender_enabled"),
                bool(updates.get("live_graduation_recommender_enabled", False)),
            )
        # PORT-LAYER-2 toggle (forven.basket_runtime).
        if "basket_funding_carry_enabled" in payload:
            updates["basket_funding_carry_enabled"] = _coerce_bool(
                payload.get("basket_funding_carry_enabled"),
                bool(updates.get("basket_funding_carry_enabled", False)),
            )
        # EQ-BASIS-1: whether the master wallet counts toward the live equity
        # basis when direction books are enabled (forven.daemon).
        if "live_equity_include_master" in payload:
            updates["live_equity_include_master"] = _coerce_bool(
                payload.get("live_equity_include_master"),
                bool(updates.get("live_equity_include_master", False)),
            )
        # SLICE-BASE-1: size live opens off the combined long+short pool
        # (forven.scanner._book_sizing_equity). Off = routed-book-only slices.
        if "live_slice_combined_books" in payload:
            updates["live_slice_combined_books"] = _coerce_bool(
                payload.get("live_slice_combined_books"),
                bool(updates.get("live_slice_combined_books", True)),
            )
        # Propr mirror per-trade risk, whole percent of the member's slice
        # (forven.propr_mirror.mirror_risk_fraction).
        if "propr_mirror_risk_pct" in payload:
            updates["propr_mirror_risk_pct"] = _coerce_float(
                payload.get("propr_mirror_risk_pct"),
                _coerce_float(updates.get("propr_mirror_risk_pct"), 2.0),
            )
        # LIQ-1: order-time liquidity guard (forven.exchange.liquidity).
        if "live_liquidity_guard_enabled" in payload:
            updates["live_liquidity_guard_enabled"] = _coerce_bool(
                payload.get("live_liquidity_guard_enabled"),
                bool(updates.get("live_liquidity_guard_enabled", True)),
            )
        for _lq_key, _lq_default in (
            ("live_min_daily_volume_usd", 5_000_000.0),
            ("live_max_spread_bps", 50.0),
            ("live_book_depth_window_bps", 100.0),
            ("live_max_book_participation_pct", 25.0),
            ("live_max_price_impact_bps", 50.0),
        ):
            if _lq_key in payload:
                updates[_lq_key] = _coerce_float(payload.get(_lq_key), _coerce_float(updates.get(_lq_key), _lq_default))
        if "strict_regime_gating" in payload:
            updates["strict_regime_gating"] = _coerce_bool(payload.get("strict_regime_gating"), updates["strict_regime_gating"])
        if "regime_min_confidence" in payload:
            updates["regime_min_confidence"] = _coerce_float(payload.get("regime_min_confidence"), updates["regime_min_confidence"])
        if "allow_unknown_regime_strategies" in payload:
            updates["allow_unknown_regime_strategies"] = _coerce_bool(payload.get("allow_unknown_regime_strategies"), updates["allow_unknown_regime_strategies"])
        # Direction×regime entry gate (REGIME-GATE-1). Same KV-blob contract as
        # the strict-regime knobs: forven.config getters read these directly.
        if "regime_gate_mode" in payload:
            _rg_mode = str(payload.get("regime_gate_mode") or "").strip().lower()
            updates["regime_gate_mode"] = _rg_mode if _rg_mode in ("off", "observe", "enforce") else "observe"
        for _rg_csv_key in ("regime_gate_block_long", "regime_gate_block_short"):
            if _rg_csv_key in payload:
                from forven.regime import normalize_regime_label as _rg_norm
                _rg_parts = [
                    _rg_norm(part)
                    for part in str(payload.get(_rg_csv_key) or "").split(",")
                ]
                updates[_rg_csv_key] = ",".join(sorted({p for p in _rg_parts if p}))
        if "regime_gate_min_confidence" in payload:
            updates["regime_gate_min_confidence"] = max(
                0.0, min(1.0, _coerce_float(payload.get("regime_gate_min_confidence"), 0.6))
            )
        # Promotion-safety gates (read top-level from forven:settings by
        # forven.policy.evaluate_promotion and forven.hypothesis_graduation).
        if "allow_unsupported_backtest_risk_controls" in payload:
            updates["allow_unsupported_backtest_risk_controls"] = _coerce_bool(
                payload.get("allow_unsupported_backtest_risk_controls"),
                bool(updates.get("allow_unsupported_backtest_risk_controls", False)),
            )
        if "canonical_requires_forward_proof" in payload:
            updates["canonical_requires_forward_proof"] = _coerce_bool(
                payload.get("canonical_requires_forward_proof"),
                bool(updates.get("canonical_requires_forward_proof", False)),
            )
        # The live trade gate (forven.regime.is_strategy_allowed -> forven.config
        # getters) now reads these keys from this KV settings blob directly, so no
        # config.json mirror is needed — every writer (UI here, and the paper
        # service) reaches the gate. Just clamp the confidence so stored == enforced.
        if "regime_min_confidence" in payload:
            updates["regime_min_confidence"] = max(0.0, min(1.0, _coerce_float(updates.get("regime_min_confidence"), 0.3)))
        if "relaxed_trade_filters_enabled" in payload:
            updates["relaxed_trade_filters_enabled"] = _coerce_bool(
                payload.get("relaxed_trade_filters_enabled"),
                bool(updates.get("relaxed_trade_filters_enabled", False)),
            )
        if "paper_test_mode_enabled" in payload:
            updates["paper_test_mode_enabled"] = _coerce_bool(
                payload.get("paper_test_mode_enabled"),
                bool(updates.get("paper_test_mode_enabled", False)),
            )
        if "paper_test_high_activity_enabled" in payload:
            updates["paper_test_high_activity_enabled"] = _coerce_bool(
                payload.get("paper_test_high_activity_enabled"),
                bool(updates.get("paper_test_high_activity_enabled", False)),
            )
        if "paper_test_bypass_gates_enabled" in payload:
            updates["paper_test_bypass_gates_enabled"] = _coerce_bool(
                payload.get("paper_test_bypass_gates_enabled"),
                bool(updates.get("paper_test_bypass_gates_enabled", False)),
            )
        if "paper_test_local_execution_only" in payload:
            updates["paper_test_local_execution_only"] = _coerce_bool(
                payload.get("paper_test_local_execution_only"),
                bool(updates.get("paper_test_local_execution_only", True)),
            )
        # PORT-DEDUP-1: cross-strategy paper clone-signal guard (read by
        # forven.scanner._open_trade_db via _scanner_bool/float_setting).
        if "paper_cross_strategy_dedup_enabled" in payload:
            updates["paper_cross_strategy_dedup_enabled"] = _coerce_bool(
                payload.get("paper_cross_strategy_dedup_enabled"),
                bool(updates.get("paper_cross_strategy_dedup_enabled", True)),
            )
        if "paper_cross_strategy_dedup_window_seconds" in payload:
            updates["paper_cross_strategy_dedup_window_seconds"] = max(
                0.0, min(86400.0, _coerce_float(
                    payload.get("paper_cross_strategy_dedup_window_seconds"), 900.0
                ))
            )

    elif section == "strategy":
        # The legacy single-strategy fields (strategy_name / strategy_symbol /
        # strategy_timeframe / strategy_parameters) were written here for years
        # and read by NOTHING — strategies have lived in their own table since
        # the container redesign. Removed 2026-07-28; only the self-healing
        # toggle in this section is real.
        if "self_healing_enabled" in payload:
            updates["self_healing_enabled"] = _coerce_bool(payload.get("self_healing_enabled"), updates["self_healing_enabled"])

    elif section == "agent-model-keys":
        if "agent_model_keys" in payload:
            updates["agent_model_keys"] = _coerce_agent_model_keys(payload.get("agent_model_keys"))
        elif "model_keys" in payload:
            updates["agent_model_keys"] = _coerce_agent_model_keys(payload.get("model_keys"))
        elif "keys" in payload:
            updates["agent_model_keys"] = _coerce_agent_model_keys(payload.get("keys"))

    elif section == "agents":
        if "backup_ai_provider" in payload:
            provider = str(payload.get("backup_ai_provider") or "none").strip().lower()
            # Only providers we can resolve a default model + credentials for.
            if provider not in {"none", "openai", "minimax", "zai", "lmstudio"}:
                provider = "none"
            updates["backup_ai_provider"] = provider
        if "backup_ai_model" in payload:
            # Empty = use the backup provider's default model.
            updates["backup_ai_model"] = str(payload.get("backup_ai_model") or "").strip()
        # A disabled backup carries no model.
        if updates.get("backup_ai_provider") == "none":
            updates["backup_ai_model"] = ""
        if "assistant_max_tool_rounds" in payload:
            updates["assistant_max_tool_rounds"] = _coerce_bounded_int(
                payload.get("assistant_max_tool_rounds"),
                updates.get("assistant_max_tool_rounds", 12),
                2,
                40,
            )

    elif section == "notifications":
        if "discord_bot_token" in payload:
            bot_token = str(payload.get("discord_bot_token") or "").strip()
            if bot_token:
                # Save main bot token to config.json (used by get_bot_token()),
                # ENCRYPTED at rest like every other secret — never plaintext. The
                # Discord webhook two blocks below already routes through the
                # encrypted secrets store; the bot token must not be the lone
                # cleartext credential on disk (audit P1.5).
                from forven.config import load_config, save_config
                from forven.secret_storage import encrypt_secret
                cfg = load_config()
                cfg["discord_token"] = encrypt_secret(bot_token)
                save_config(cfg)
        if "discord_webhook_url" in payload:
            webhook_url = str(payload.get("discord_webhook_url") or "").strip()
            if webhook_url:
                secrets["discord_webhook_url"] = webhook_url
            else:
                secrets.pop("discord_webhook_url", None)
        if "notification_level" in payload:
            updates["notification_level"] = str(payload.get("notification_level") or updates["notification_level"]).strip()
        if "notify_on_entry" in payload:
            updates["notify_on_entry"] = _coerce_bool(payload.get("notify_on_entry"), updates["notify_on_entry"])
        if "notify_on_exit" in payload:
            updates["notify_on_exit"] = _coerce_bool(payload.get("notify_on_exit"), updates["notify_on_exit"])
        if "notify_daily_summary" in payload:
            updates["notify_daily_summary"] = _coerce_bool(payload.get("notify_daily_summary"), updates["notify_daily_summary"])
        if "notify_health_reports" in payload:
            updates["notify_health_reports"] = _coerce_bool(payload.get("notify_health_reports"), updates["notify_health_reports"])
        if "notify_errors" in payload:
            updates["notify_errors"] = _coerce_bool(payload.get("notify_errors"), updates["notify_errors"])
        # Bridge the UI toggles into the REAL delivery gate
        # (forven:notification_preferences), which resolve_notification_policy
        # actually reads. Writing only the flat KV keys above changes nothing
        # that gets delivered to Discord. Mirrors the regime-gating pattern.
        _notif_pref_updates: dict[str, object] = {}
        for _toggle, _pref_keys in _NOTIF_TOGGLE_PREF_KEYS.items():
            if _toggle in payload:
                _val = _coerce_bool(payload.get(_toggle), True)
                for _pk in _pref_keys:
                    _notif_pref_updates[_pk] = _val
        if _notif_pref_updates or "notification_level" in payload:
            try:
                from forven.notifications import (
                    get_notification_preferences,
                    update_notification_preferences,
                )
                _existing_prefs = get_notification_preferences()
                if "notification_level" in payload:
                    _level = str(payload.get("notification_level") or "all").strip().lower()
                    if _level == "none":
                        _notif_pref_updates["discord_mode"] = "shadow"
                    else:
                        # Preserve a manually-set 'legacy' (force-deliver-all) mode;
                        # only (re)assert 'policy' when not already legacy.
                        _cur_mode = str(_existing_prefs.get("discord_mode") or "policy").strip().lower()
                        _notif_pref_updates["discord_mode"] = _cur_mode if _cur_mode == "legacy" else "policy"
                update_notification_preferences({**_existing_prefs, **_notif_pref_updates})
            except Exception as exc:
                log.warning("Could not mirror notification toggles to preferences store: %s", exc)

    elif section == "bot-operations":
        if "scanner_execution_enabled" in payload:
            updates["scanner_execution_enabled"] = _coerce_bool(
                payload.get("scanner_execution_enabled"),
                bool(updates.get("scanner_execution_enabled", True)),
            )
        if "auto_restart_on_crash" in payload:
            updates["auto_restart_on_crash"] = _coerce_bool(payload.get("auto_restart_on_crash"), updates["auto_restart_on_crash"])
        if "auto_approve_code_edits" in payload:
            updates["auto_approve_code_edits"] = _coerce_bool(payload.get("auto_approve_code_edits"), updates.get("auto_approve_code_edits", False))
        if "auto_approve_promotions" in payload:
            updates["auto_approve_promotions"] = _coerce_bool(payload.get("auto_approve_promotions"), updates.get("auto_approve_promotions", False))
        if "allow_auto_live_promotion" in payload:
            updates["allow_auto_live_promotion"] = _coerce_bool(payload.get("allow_auto_live_promotion"), updates.get("allow_auto_live_promotion", False))
        if "auto_approve_dethrone" in payload:
            updates["auto_approve_dethrone"] = _coerce_bool(payload.get("auto_approve_dethrone"), updates.get("auto_approve_dethrone", True))
        if "canonical_auto_deploy_enabled" in payload:
            updates["canonical_auto_deploy_enabled"] = _coerce_bool(payload.get("canonical_auto_deploy_enabled"), updates.get("canonical_auto_deploy_enabled", False))
        if "paper_slot_competition_enabled" in payload:
            updates["paper_slot_competition_enabled"] = _coerce_bool(payload.get("paper_slot_competition_enabled"), updates.get("paper_slot_competition_enabled", False))
        if "brain_queue_max_pending" in payload:
            updates["brain_queue_max_pending"] = _coerce_optional_int(payload.get("brain_queue_max_pending"), updates.get("brain_queue_max_pending", 15))
        if "maintenance_start_hour" in payload:
            updates["maintenance_start_hour"] = _coerce_optional_int(payload.get("maintenance_start_hour"))
        if "maintenance_end_hour" in payload:
            updates["maintenance_end_hour"] = _coerce_optional_int(payload.get("maintenance_end_hour"))
        if "data_refresh_seconds" in payload:
            updates["data_refresh_seconds"] = _coerce_optional_int(payload.get("data_refresh_seconds"), updates["data_refresh_seconds"])
        if "throughput_auto_scheduler_control" in payload:
            updates["throughput_auto_scheduler_control"] = _coerce_bool(
                payload.get("throughput_auto_scheduler_control"),
                bool(updates.get("throughput_auto_scheduler_control", True)),
            )
        if "adaptive_pipeline_throughput_enabled" in payload:
            updates["adaptive_pipeline_throughput_enabled"] = _coerce_bool(
                payload.get("adaptive_pipeline_throughput_enabled"),
                bool(updates.get("adaptive_pipeline_throughput_enabled", False)),
            )
        if "pipeline_target_clear_hours" in payload:
            updates["pipeline_target_clear_hours"] = _coerce_bounded_int(
                payload.get("pipeline_target_clear_hours"),
                _coerce_bounded_int(updates.get("pipeline_target_clear_hours"), 6, 1, 168),
                1,
                168,
            )
        if "ideation_interval_minutes" in payload:
            updates["ideation_interval_minutes"] = _coerce_bounded_int(
                payload.get("ideation_interval_minutes"),
                _coerce_bounded_int(updates.get("ideation_interval_minutes"), 1440, 1, 1440),
                1,
                1440,
            )
        if "coding_interval_minutes" in payload:
            updates["coding_interval_minutes"] = _coerce_bounded_int(
                payload.get("coding_interval_minutes"),
                _coerce_bounded_int(updates.get("coding_interval_minutes"), 1440, 1, 1440),
                1,
                1440,
            )
        if "testing_interval_minutes" in payload:
            updates["testing_interval_minutes"] = _coerce_bounded_int(
                payload.get("testing_interval_minutes"),
                _coerce_bounded_int(updates.get("testing_interval_minutes"), 1440, 1, 1440),
                1,
                1440,
            )
        if "graduation_interval_minutes" in payload:
            updates["graduation_interval_minutes"] = _coerce_bounded_int(
                payload.get("graduation_interval_minutes"),
                _coerce_bounded_int(updates.get("graduation_interval_minutes"), 1440, 1, 10080),
                1,
                10080,
            )
        if "scanner_signal_interval_minutes" in payload:
            updates["scanner_signal_interval_minutes"] = _coerce_bounded_int(
                payload.get("scanner_signal_interval_minutes"),
                _coerce_bounded_int(updates.get("scanner_signal_interval_minutes"), 5, 1, 1440),
                1,
                1440,
            )
        if "scanner_execution_interval_minutes" in payload:
            updates["scanner_execution_interval_minutes"] = _coerce_bounded_int(
                payload.get("scanner_execution_interval_minutes"),
                _coerce_bounded_int(updates.get("scanner_execution_interval_minutes"), 5, 1, 1440),
                1,
                1440,
            )
        if "scanner_allow_direct_market_fetch" in payload:
            updates["scanner_allow_direct_market_fetch"] = _coerce_bool(
                payload.get("scanner_allow_direct_market_fetch"),
                bool(updates.get("scanner_allow_direct_market_fetch", True)),
            )
        if "market_data_source" in payload:
            _src = str(payload.get("market_data_source") or "").strip().lower()
            updates["market_data_source"] = _src if _src in ("binance", "hyperliquid") else "binance"
        if "daemon_candle_cache_refresh_seconds" in payload:
            updates["daemon_candle_cache_refresh_seconds"] = _coerce_bounded_int(
                payload.get("daemon_candle_cache_refresh_seconds"),
                _coerce_bounded_int(updates.get("daemon_candle_cache_refresh_seconds"), 90, 15, 3600),
                15,
                3600,
            )
        if "paper_test_mode_enabled" in payload:
            updates["paper_test_mode_enabled"] = _coerce_bool(
                payload.get("paper_test_mode_enabled"),
                bool(updates.get("paper_test_mode_enabled", False)),
            )
        if "paper_test_high_activity_enabled" in payload:
            updates["paper_test_high_activity_enabled"] = _coerce_bool(
                payload.get("paper_test_high_activity_enabled"),
                bool(updates.get("paper_test_high_activity_enabled", False)),
            )
        if "paper_test_bypass_gates_enabled" in payload:
            updates["paper_test_bypass_gates_enabled"] = _coerce_bool(
                payload.get("paper_test_bypass_gates_enabled"),
                bool(updates.get("paper_test_bypass_gates_enabled", False)),
            )
        if "paper_test_local_execution_only" in payload:
            updates["paper_test_local_execution_only"] = _coerce_bool(
                payload.get("paper_test_local_execution_only"),
                bool(updates.get("paper_test_local_execution_only", True)),
            )
        if "pipeline_assignments_per_cycle" in payload:
            updates["pipeline_assignments_per_cycle"] = _coerce_bounded_int(
                payload.get("pipeline_assignments_per_cycle"),
                _coerce_bounded_int(updates.get("pipeline_assignments_per_cycle"), 3, 1, 20),
                1,
                20,
            )
        if "pipeline_drain_mode" in payload:
            updates["pipeline_drain_mode"] = _coerce_bool(
                payload.get("pipeline_drain_mode"),
                bool(updates.get("pipeline_drain_mode", True)),
            )
        if "pipeline_drain_max_seconds" in payload:
            updates["pipeline_drain_max_seconds"] = _coerce_bounded_int(
                payload.get("pipeline_drain_max_seconds"),
                _coerce_bounded_int(updates.get("pipeline_drain_max_seconds"), 300, 30, 1800),
                30,
                1800,
            )
        if "pipeline_gate_failure_archive_attempts" in payload:
            updates["pipeline_gate_failure_archive_attempts"] = _coerce_bounded_int(
                payload.get("pipeline_gate_failure_archive_attempts"),
                _coerce_bounded_int(updates.get("pipeline_gate_failure_archive_attempts"), 3, 1, 10),
                1,
                10,
            )
        if "backtest_matrix_workers" in payload:
            updates["backtest_matrix_workers"] = _coerce_bounded_int(
                payload.get("backtest_matrix_workers"),
                _coerce_bounded_int(updates.get("backtest_matrix_workers"), 4, 1, 8),
                1,
                8,
            )
        if "backtest_subprocess_budget" in payload:
            updates["backtest_subprocess_budget"] = _coerce_bounded_int(
                payload.get("backtest_subprocess_budget"),
                _coerce_bounded_int(updates.get("backtest_subprocess_budget"), 4, 1, 8),
                1,
                8,
            )
        if "gauntlet_drain_workers" in payload:
            updates["gauntlet_drain_workers"] = _coerce_bounded_int(
                payload.get("gauntlet_drain_workers"),
                _coerce_bounded_int(updates.get("gauntlet_drain_workers"), 3, 1, 8),
                1,
                8,
            )
        if "pipeline_saturation_threshold" in payload:
            updates["pipeline_saturation_threshold"] = _coerce_bounded_int(
                payload.get("pipeline_saturation_threshold"),
                _coerce_bounded_int(updates.get("pipeline_saturation_threshold"), 100, 10, 500),
                10,
                500,
            )
        if "pipeline_resume_threshold" in payload:
            updates["pipeline_resume_threshold"] = _coerce_bounded_int(
                payload.get("pipeline_resume_threshold"),
                _coerce_bounded_int(updates.get("pipeline_resume_threshold"), 60, 5, 400),
                5,
                400,
            )
        if "agent_task_claim_limit" in payload:
            updates["agent_task_claim_limit"] = _coerce_bounded_int(
                payload.get("agent_task_claim_limit"),
                _coerce_bounded_int(updates.get("agent_task_claim_limit"), 6, 1, 20),
                1,
                20,
            )
        if "brain_task_claim_limit" in payload:
            updates["brain_task_claim_limit"] = _coerce_bounded_int(
                payload.get("brain_task_claim_limit"),
                _coerce_bounded_int(updates.get("brain_task_claim_limit"), 6, 1, 20),
                1,
                20,
            )
        if "code_strategy_requires_approval" in payload:
            updates["code_strategy_requires_approval"] = _coerce_bool(
                payload.get("code_strategy_requires_approval"),
                bool(updates.get("code_strategy_requires_approval", False)),
            )
        if "task_stale_recovery_minutes" in payload:
            updates["task_stale_recovery_minutes"] = _coerce_bounded_int(
                payload.get("task_stale_recovery_minutes"),
                _coerce_bounded_int(updates.get("task_stale_recovery_minutes"), 10, 1, 1440),
                1,
                1440,
            )
        if "remote_engine_enabled" in payload:
            updates["remote_engine_enabled"] = _coerce_bool(payload.get("remote_engine_enabled"), bool(updates.get("remote_engine_enabled", False)))
        if "remote_engine_url" in payload:
            updates["remote_engine_url"] = str(payload.get("remote_engine_url") or "").strip()
        if "remote_engine_data_root" in payload:
            updates["remote_engine_data_root"] = str(payload.get("remote_engine_data_root") or "").strip()

    elif section == "health-checks":
        if "enabled" in payload:
            updates["health_checks_enabled"] = _coerce_bool(payload.get("enabled"), updates["health_checks_enabled"])
        if "rolling_backtest_days" in payload:
            updates["rolling_backtest_days"] = _coerce_optional_int(payload.get("rolling_backtest_days"), updates["rolling_backtest_days"])
        if "walkforward_months" in payload:
            updates["walkforward_months"] = _coerce_optional_int(payload.get("walkforward_months"), updates["walkforward_months"])
        if "walkforward_folds" in payload:
            updates["walkforward_folds"] = _coerce_optional_int(payload.get("walkforward_folds"), updates["walkforward_folds"])
        if "regime_detection_enabled" in payload:
            updates["regime_detection_enabled"] = _coerce_bool(payload.get("regime_detection_enabled"), updates["regime_detection_enabled"])
        if "relaxed_trade_filters_enabled" in payload:
            updates["relaxed_trade_filters_enabled"] = _coerce_bool(
                payload.get("relaxed_trade_filters_enabled"),
                bool(updates.get("relaxed_trade_filters_enabled", False)),
            )
        if "alert_on_degradation_pct" in payload:
            updates["alert_on_degradation_pct"] = _coerce_float(payload.get("alert_on_degradation_pct"), updates["alert_on_degradation_pct"])

    elif section == "backtesting-defaults":
        if "backtest_fee_bps" in payload:
            updates["backtest_fee_bps"] = _coerce_float(payload.get("backtest_fee_bps"), updates.get("backtest_fee_bps", 4.5))
        if "backtest_slippage_bps" in payload:
            updates["backtest_slippage_bps"] = _coerce_float(payload.get("backtest_slippage_bps"), updates.get("backtest_slippage_bps", 2.0))
        if "default_leverage" in payload:
            _lev = _coerce_float(payload.get("default_leverage"), updates.get("default_leverage", 1.0))
            updates["default_leverage"] = float(_lev) if (_lev is not None and _lev > 0) else 1.0
        if "backtest_timeframe" in payload:
            updates["backtest_timeframe"] = str(payload.get("backtest_timeframe") or "1h").strip()
        if "backtest_symbol" in payload:
            updates["backtest_symbol"] = str(payload.get("backtest_symbol") or "BTC/USDT").strip()
        if "backtest_duration_days" in payload:
            updates["backtest_duration_days"] = _coerce_optional_int(payload.get("backtest_duration_days"), updates.get("backtest_duration_days", DEFAULT_BACKTEST_DURATION_DAYS))
        # Per-stage backtest windows; 0 = inherit the global default above.
        for _stage_key in (
            "quick_screen_duration_days",
            "timeframe_sweep_duration_days",
            "optimization_duration_days",
            "confirmation_duration_days",
            "walk_forward_duration_days",
            "cost_stress_duration_days",
            "evolution_duration_days",
        ):
            if _stage_key in payload:
                updates[_stage_key] = _coerce_optional_int(
                    payload.get(_stage_key), updates.get(_stage_key, 0)
                )
        if "rolling_backtest_days" in payload:
            updates["rolling_backtest_days"] = _coerce_optional_int(payload.get("rolling_backtest_days"), updates.get("rolling_backtest_days", 30))
        if "walkforward_months" in payload:
            updates["walkforward_months"] = _coerce_optional_int(payload.get("walkforward_months"), updates.get("walkforward_months", 6))
        if "walkforward_folds" in payload:
            updates["walkforward_folds"] = _coerce_optional_int(payload.get("walkforward_folds"), updates.get("walkforward_folds", 5))
        if "walkforward_cv_method" in payload:
            updates["walkforward_cv_method"] = str(payload.get("walkforward_cv_method") or "rolling").strip()
        if "walkforward_train_ratio" in payload:
            updates["walkforward_train_ratio"] = _coerce_float(payload.get("walkforward_train_ratio"), updates.get("walkforward_train_ratio", 0.7))
        if "walkforward_purge_gap" in payload:
            updates["walkforward_purge_gap"] = _coerce_optional_int(payload.get("walkforward_purge_gap"), updates.get("walkforward_purge_gap", 0))
        if "walkforward_embargo_pct" in payload:
            updates["walkforward_embargo_pct"] = _coerce_float(payload.get("walkforward_embargo_pct"), updates.get("walkforward_embargo_pct", 0))
        if "walkforward_objective" in payload:
            updates["walkforward_objective"] = str(payload.get("walkforward_objective") or "sharpe_ratio").strip()
        if "walkforward_n_trials" in payload:
            updates["walkforward_n_trials"] = _coerce_optional_int(payload.get("walkforward_n_trials"), updates.get("walkforward_n_trials", 50))
        if "backtest_include_funding" in payload:
            updates["backtest_include_funding"] = _coerce_bool(
                payload.get("backtest_include_funding"),
                bool(updates.get("backtest_include_funding", True)),
            )

    elif section == "research":
        raw_research_settings = payload.get("research_settings")
        if not isinstance(raw_research_settings, dict):
            raw_research_settings = payload
        stored_research_settings = updates.get("research_settings")
        if not isinstance(stored_research_settings, dict):
            stored_research_settings = {}
        # DEEP-merge the incoming partial over STORED values (the UI sends only
        # the edited leaves). The previous shallow spread replaced whole nested
        # dicts, so editing e.g. hypothesis_discipline.crucible_daily_develop_budget
        # would silently reset its customized siblings back to defaults.
        updates["research_settings"] = _merge_research_settings_payload(
            _deep_merge_dicts(stored_research_settings, dict(raw_research_settings or {}))
        )

    elif section in {"data-engine", "data_engine"}:
        raw_data_engine_settings = payload.get("data_engine_settings")
        if not isinstance(raw_data_engine_settings, dict):
            raw_data_engine_settings = payload
        stored_data_engine_settings = updates.get("data_engine_settings")
        if not isinstance(stored_data_engine_settings, dict):
            stored_data_engine_settings = {}
        # DEEP-merge the incoming partial over STORED values (the UI sends only
        # the edited leaves). A shallow spread here used to replace whole nested
        # dicts, so editing e.g. source_reconciliation.max_divergence_pct would
        # silently reset source_reconciliation.enabled back to its default.
        updates["data_engine_settings"] = _merge_data_engine_settings_payload(
            _deep_merge_dicts(stored_data_engine_settings, dict(raw_data_engine_settings or {}))
        )

    elif section == "ui":
        if "setup_wizard_completed_at" in payload:
            value = payload.get("setup_wizard_completed_at")
            if value is None:
                updates["setup_wizard_completed_at"] = None
            elif isinstance(value, str):
                updates["setup_wizard_completed_at"] = value.strip() or None
            else:
                raise HTTPException(
                    status_code=400,
                    detail="setup_wizard_completed_at must be a string or null",
                )

    else:
        raise HTTPException(status_code=400, detail=f"Unsupported settings section: {section}")

    updates["hyperliquid_has_key"] = bool(str(secrets.get("hyperliquid_private_key", "")).strip())
    updates["discord_webhook_configured"] = bool(str(secrets.get("discord_webhook_url", "")).strip())
    try:
        import os as _os
        from forven.config import load_config as _load_cfg
        _cfg = _load_cfg()
        _has_config_token = bool(str(_cfg.get("discord_token", "")).strip())
        _has_env_token = bool(str(_os.environ.get("DISCORD_TOKEN", "")).strip())
        updates["discord_bot_token_configured"] = _has_config_token or _has_env_token
        updates["discord_bot_token_source"] = "config" if _has_config_token else ("env" if _has_env_token else "none")
    except Exception:
        updates["discord_bot_token_configured"] = False
        updates["discord_bot_token_source"] = "none"

    updates["updated_at"] = _now()

    # Compute the audit diff against the pre-mutation snapshot and fold the
    # audit_log into the SAME blob we persist. `old_snapshot` carries the prior
    # audit_log (in _AUDIT_IGNORE_KEYS, so it never self-diffs); append this
    # request's entries onto it before writing.
    entries = _diff_settings_section(section, old_snapshot, updates, actor=actor)
    if entries:
        updates["audit_log"] = _append_settings_audit(
            old_snapshot.get("audit_log") or [], entries
        )

    # Persist the encrypted secrets blob AND the main settings blob (audit
    # entry included) in ONE transaction: a crash between them can no longer
    # leave enforcement diverged from display. Replaces the two separate
    # _save_settings_secrets / _save_settings_payload calls that used to run
    # here and the third blob save the endpoint did after appending audit.
    kv_set_many({
        _SETTINGS_SECRET_STORAGE_KEY: _encrypt_settings_secrets(secrets),
        _SETTINGS_STORAGE_KEY: updates,
    })

    if section == "bot-operations":
        try:
            from forven.scheduler import apply_runtime_scheduler_overrides
            apply_runtime_scheduler_overrides()
        except Exception as exc:
            log.warning("Could not apply scheduler runtime overrides after settings update: %s", exc)

    # When the user saves HyperLiquid credentials, stale `recovery_active=True`
    # from pre-save reconcile failures would otherwise linger in daemon_state
    # until the next periodic reconcile (10 min). Clear it now so the "TRADING
    # HALTED" banner goes away as soon as credentials are valid.
    if section == "hyperliquid" and bool(updates.get("hyperliquid_has_key")):
        try:
            from forven.exchange.hyperliquid import _get_creds
            _get_creds()
            daemon_state = kv_get("daemon_state", {}) or {}
            if isinstance(daemon_state, dict) and daemon_state.get("recovery_active"):
                last_err = str(daemon_state.get("last_reconcile_error") or "")
                summary = str(daemon_state.get("recovery_summary") or "")
                if "private key" in (last_err + summary).lower() or "credentials" in (last_err + summary).lower():
                    daemon_state["recovery_active"] = False
                    daemon_state["recovery_status"] = "credentials_updated"
                    daemon_state["recovery_requires_operator"] = False
                    daemon_state["recovery_summary"] = (
                        "HyperLiquid credentials updated — awaiting next reconcile."
                    )
                    daemon_state["last_reconcile_error"] = None
                    kv_set("daemon_state", daemon_state)
        except Exception as exc:
            log.debug("Could not clear stale daemon recovery state after hyperliquid save: %s", exc)

    return updates


def _normalize_status(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return normalized or None


def _to_core_status(state: str | None) -> str | None:
    """Map lifecycle-style states to canonical strategy statuses used by `strategies`."""
    if state is None:
        return None
    return normalize_stage(state)


def _to_lifecycle_state(core_status: str | None) -> str:
    """Map strategy status to lifecycle state names consumed by lifecycle UI clients."""
    normalized = normalize_stage(core_status)

    core_to_lifecycle = {
        "quick_screen": "generated",
        "research_only": "research_only",
        "gauntlet": "backtesting",
        "paper": "paper",
        "live_graduated": "deployed",
        "retired": "retired",
        "archived": "retired",
        "rejected": "rejected",
    }

    if normalized in core_to_lifecycle:
        return core_to_lifecycle[normalized]

    if normalized == "research_only":
        return "research_only"
    if normalized.startswith("paper") or normalized == "paper_trading":
        return "paper"
    if normalized.startswith("backtest") or normalized == "gauntlet":
        return "backtesting"
    if normalized.startswith("deploy") or normalized.startswith("live"):
        return "deployed"
    if normalized.startswith("research") or normalized.startswith("quick"):
        return "generated"

    return "generated"


def _normalize_lifecycle_metrics(raw_metrics) -> dict:
    """Normalize legacy strategy metrics into a dictionary for lifecycle response."""
    if raw_metrics is None:
        return {}

    if isinstance(raw_metrics, str):
        try:
            raw_metrics = json.loads(raw_metrics)
        except Exception:
            return {}

    if not isinstance(raw_metrics, dict):
        return {}

    metrics = dict(raw_metrics)

    alias_pairs = {
        "winRate": "win_rate",
        "sharpe": "sharpe_ratio",
        "profitFactor": "profit_factor",
        "totalReturn": "total_return",
        "maxDrawdown": "max_drawdown",
        "totalTrades": "total_trades",
        "sortinoRatio": "sortino_ratio",
        "calmarRatio": "calmar_ratio",
    }

    for source, target in alias_pairs.items():
        if target not in metrics and source in metrics:
            metrics[target] = metrics[source]

    # Clamp drawdown values to [0, 1] â€” legacy data may contain values > 1.0.
    for dd_key in ("max_drawdown_pct", "max_drawdown"):
        if dd_key in metrics and isinstance(metrics[dd_key], (int, float)):
            metrics[dd_key] = max(0.0, min(1.0, abs(metrics[dd_key])))

    # Also clamp nested in_sample / out_of_sample drawdown values.
    for nested_key in ("in_sample", "out_of_sample"):
        nested = metrics.get(nested_key)
        if isinstance(nested, dict):
            for dd_key in ("max_drawdown_pct", "max_drawdown"):
                if dd_key in nested and isinstance(nested[dd_key], (int, float)):
                    nested[dd_key] = max(0.0, min(1.0, abs(nested[dd_key])))

    return metrics


def _row_to_lifecycle_strategy(row: dict) -> dict:
    """Convert a legacy `strategies` row to a lifecycle-style strategy payload."""
    strategy_id = str((row or {}).get("id") or "").strip()
    display_id = str((row or {}).get("display_id") or "").strip() or None
    status = str((row or {}).get("stage") or (row or {}).get("status") or "quick_screen")
    strategy_name = str((row or {}).get("name") or strategy_id or "Unnamed Strategy")
    created_at = str((row or {}).get("created_at") or _now())
    updated_at = str((row or {}).get("updated_at") or created_at)
    state_changed_at = str((row or {}).get("stage_changed_at") or updated_at)
    params = (row or {}).get("params")
    if not isinstance(params, str) and params is not None:
        try:
            params = json.dumps(params)
        except Exception:
            params = None

    metrics = _normalize_lifecycle_metrics((row or {}).get("metrics"))

    return {
        "id": strategy_id,
        "display_id": display_id,
        "name": strategy_name,
        "state": _to_lifecycle_state(status),
        "source": "core",
        "source_ref": strategy_id,
        "symbol": (row or {}).get("symbol") or None,
        "timeframe": (row or {}).get("timeframe") or None,
        "definition_json": params,
        "dataset_hash": None,
        "policy_version": 1,
        "build_version": None,
        "metrics_json": json.dumps(metrics) if metrics else None,
        "metrics": metrics,
        "paper_session_id": None,
        "paper_started_at": None,
        "last_policy_result_json": None,
        "blocked_reason": (row or {}).get("notes") or None,
        "model": (row or {}).get("model") or None,
        "model_id": (row or {}).get("model_id") or None,
        "created_at": created_at,
        "updated_at": updated_at,
        "state_changed_at": state_changed_at,
        "failed_at": None,
        "retention_expires_at": None,
    }


def _normalize_lifecycle_event_row(event_row: dict) -> dict:
    """Normalize core lifecycle event rows for lifecycle API clients."""
    row = dict(event_row or {})
    row["from_state"] = _to_lifecycle_state(row.get("from_state"))
    row["to_state"] = _to_lifecycle_state(row.get("to_state"))
    return row


# ARCH-06: the POST-body models that were defined here now live in
# forven.api_models (re-exported at the top of this file). These three are not
# definitions — they alias models OWNED by forven.strategy_lifecycle — so they
# stay here rather than making api_models import the lifecycle service.
StrategyPromoteBody = lifecycle_service.StrategyPromoteBody
LifecycleTransitionBody = lifecycle_service.LifecycleTransitionBody
LifecycleCreateBody = lifecycle_service.LifecycleCreateBody


def _coerce_profile_expiry(body: AuthProviderProfileBody) -> int | None:
    if body.expires_in is not None:
        try:
            seconds = float(body.expires_in)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid expires_in: {body.expires_in}") from exc
        return int(time.time() * 1000 + (seconds * 1000))

    if body.expires_at is None:
        return None

    parsed = _coerce_expiry_ms(body.expires_at)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"invalid expires_at: {body.expires_at}")
    return parsed


def _shutdown_session_listener(session: dict[str, object] | None) -> None:
    if not isinstance(session, dict):
        return
    listener = session.get("listener")
    if listener is None:
        return
    try:
        listener.shutdown()
    except Exception:
        pass


def _prune_auth_oauth_sessions() -> None:
    cutoff = time.time() - _AUTH_OAUTH_SESSION_TTL_SECONDS
    for provider, sessions in list(_AUTH_OAUTH_SESSIONS.items()):
        for state, details in list(sessions.items()):
            created_at = details.get("created_at")
            if not isinstance(created_at, (int, float)) or created_at < cutoff:
                expired = sessions.pop(state, None)
                _shutdown_session_listener(expired)
                _AUTH_OAUTH_CALLBACKS.get(provider, {}).pop(state, None)
        if not sessions:
            _AUTH_OAUTH_SESSIONS.pop(provider, None)
        if not _AUTH_OAUTH_CALLBACKS.get(provider):
            _AUTH_OAUTH_CALLBACKS.pop(provider, None)


def _store_oauth_session(provider: str, state: str, details: dict[str, object]) -> None:
    _prune_auth_oauth_sessions()
    provider_key = provider.lower()
    _AUTH_OAUTH_RESULTS.get(provider_key, {}).pop(str(state), None)
    sessions = _AUTH_OAUTH_SESSIONS.setdefault(provider_key, {})
    payload = dict(details)
    payload["created_at"] = time.time()
    sessions[str(state)] = payload


def _consume_oauth_session(provider: str, state: str | None) -> dict[str, object] | None:
    if not isinstance(state, str):
        return None
    _prune_auth_oauth_sessions()
    provider_key = provider.lower()
    sessions = _AUTH_OAUTH_SESSIONS.get(provider_key, {})
    session = sessions.pop(state, None)
    _AUTH_OAUTH_CALLBACKS.get(provider_key, {}).pop(state, None)
    if not isinstance(session, dict):
        return None
    return session


def _store_oauth_result(provider: str, state: str | None, result: dict[str, object]) -> None:
    if not isinstance(state, str) or not state:
        return
    _AUTH_OAUTH_RESULTS.setdefault(provider.lower(), {})[state] = dict(result)


def _peek_oauth_result(provider: str, state: str | None) -> dict[str, object] | None:
    if not isinstance(state, str):
        return None
    result = _AUTH_OAUTH_RESULTS.get(provider.lower(), {}).get(state)
    if not isinstance(result, dict):
        return None
    return dict(result)


def _peek_oauth_session(provider: str, state: str | None) -> dict[str, object] | None:
    if not isinstance(state, str):
        return None
    _prune_auth_oauth_sessions()
    provider_key = provider.lower()
    sessions = _AUTH_OAUTH_SESSIONS.get(provider_key, {})
    session = sessions.get(state)
    if not isinstance(session, dict):
        return None
    return session


def _record_oauth_callback(provider: str, code: str, state: str | None) -> None:
    if not state:
        return
    normalized_code = _coerce_oauth_code(code)
    if not normalized_code:
        return
    provider_key = provider.lower()
    _AUTH_OAUTH_CALLBACKS.setdefault(provider_key, {})[state] = normalized_code
    session = _peek_oauth_session(provider_key, state)
    if session is not None:
        session["callback_code"] = normalized_code


def _finalize_openai_callback(code: str, state: str | None) -> None:
    if not state:
        return
    normalized_code = _coerce_oauth_code(code)
    if not normalized_code:
        _store_oauth_result("openai", state, {"status": "error", "error": "missing oauth code"})
        return

    _record_oauth_callback("openai", normalized_code, state)
    session = _peek_oauth_session("openai", state)
    if not session:
        if _peek_oauth_result("openai", state) is None:
            _store_oauth_result("openai", state, {"status": "expired"})
        return

    if session.get("completion_started"):
        return
    session["completion_started"] = True
    listener = session.get("listener")
    verifier = str(session.get("code_verifier") or "")

    try:
        _complete_openai_oauth(state, normalized_code, verifier)
    except HTTPException as exc:
        _store_oauth_result("openai", state, {"status": "error", "error": str(exc.detail)})
        log.warning("openai oauth callback completion failed: %s", exc.detail)
    except Exception as exc:
        _store_oauth_result("openai", state, {"status": "error", "error": str(exc)})
        log.exception("openai oauth callback completion failed")
    else:
        _store_oauth_result("openai", state, {"status": "complete"})
    finally:
        _shutdown_session_listener({"listener": listener})


def _finalize_openai_callback_async(code: str, state: str | None) -> None:
    threading.Thread(
        target=_finalize_openai_callback,
        args=(code, state),
        daemon=True,
        name="openai-oauth-finalize",
    ).start()


def _coerce_oauth_code(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""

    candidates = [raw]
    if not raw.startswith("http://") and not raw.startswith("https://"):
        candidates.append(f"http://127.0.0.1/callback?{raw.lstrip('?')}")

    if "code=" in raw:
        for candidate in candidates:
            try:
                parsed = urllib.parse.urlparse(candidate)
                params = urllib.parse.parse_qs(parsed.query)
                code_values = params.get("code")
                if isinstance(code_values, list) and code_values:
                    return str(code_values[0]).strip()
            except Exception:
                continue

            if "#" in candidate:
                fragment = parsed.fragment
                fragment_params = urllib.parse.parse_qs(fragment)
                fragment_code = fragment_params.get("code")
                if isinstance(fragment_code, list) and fragment_code:
                    return str(fragment_code[0]).strip()

    if raw.startswith("code="):
        return raw.split("code=", 1)[1].split("&", 1)[0]

    # Handle code#state format (some OAuth callbacks append state after #)
    if "#" in raw and "code=" not in raw:
        return raw.split("#", 1)[0].strip()

    return raw


def _http_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        text = (response.text or "").strip()
        return text or "no details"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or error.get("error") or "").strip()
            if detail:
                return detail
        if isinstance(error, str) and error:
            return error
        detail = payload.get("detail")
        if isinstance(detail, str) and detail:
            return detail
    return str(payload)


def _build_openai_oauth_start() -> dict[str, object]:
    from forven.auth import openai as openai_auth

    verifier, challenge = generate_pkce()
    state = generate_state()
    authorize_url = openai_auth._build_auth_url(state, challenge)

    listener = LoopbackCallbackListener(
        port=_OPENAI_LOOPBACK_PORT,
        ttl_seconds=_OPENAI_OAUTH_LISTENER_TTL_SECONDS,
        on_callback=lambda code, callback_state: _finalize_openai_callback_async(
            code,
            callback_state,
        ),
    )
    auto_callback = listener.start()

    session_payload: dict[str, object] = {
        "provider": "openai",
        "code_verifier": verifier,
        "flow": "authorization_code",
        "auto_callback": auto_callback,
    }
    if auto_callback:
        session_payload["listener"] = listener

    _store_oauth_session("openai", state, session_payload)

    response: dict[str, object] = {
        "provider": "openai",
        "flow": "authorization_code",
        "state": state,
        "authorize_url": authorize_url,
        "auto_callback": auto_callback,
    }
    if not auto_callback:
        response["code_verifier"] = verifier
        response["bind_error"] = listener.bind_error or "port_in_use"
    return response


def _build_minimax_oauth_start() -> dict[str, str]:
    from forven.auth import minimax as minimax_auth

    verifier, challenge = generate_pkce()
    state = generate_state()
    payload = {
        "response_type": "code",
        "client_id": minimax_auth.CLIENT_ID,
        "scope": minimax_auth.SCOPES,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    # follow_redirects: MiniMax's oauth/code 307-redirects to account.minimax.io;
    # without following it the body is empty and .json() raised an unhandled 500.
    try:
        code_response = httpx.post(
            minimax_auth.CODE_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
            follow_redirects=True,
        )
        code_response.raise_for_status()
        code_payload = code_response.json()
    except httpx.HTTPError as exc:
        log.warning("minimax oauth code endpoint failed: %s", exc)
        raise HTTPException(status_code=502, detail="unable to reach the MiniMax OAuth endpoint") from exc
    except ValueError as exc:
        log.warning("minimax oauth code endpoint returned a non-JSON response: %s", exc)
        raise HTTPException(status_code=502, detail="MiniMax OAuth endpoint returned an unexpected response") from exc

    verification_url = str(code_payload.get("verification_url") or code_payload.get("verification_uri") or "")
    user_code = str(code_payload.get("user_code") or "").strip()
    interval = int(code_payload.get("interval", 2000)) / 1000.0 if int(code_payload.get("interval", 2)) > 100 else int(code_payload.get("interval", 2))
    expires_in = int(code_payload.get("expires_in", 600))
    if not verification_url or not user_code:
        raise HTTPException(status_code=400, detail="failed to initialize minimax oauth flow")

    _store_oauth_session("minimax", state, {
        "provider": "minimax",
        "code_verifier": verifier,
        "flow": "device_code",
        "user_code": user_code,
        "verification_url": verification_url,
        "interval": interval,
        "attempts": 0,
        "max_attempts": int(max(1, expires_in // max(interval, 1))),
    })

    return {
        "provider": "minimax",
        "flow": "device_code",
        "state": state,
        "verification_url": verification_url,
        "user_code": user_code,
        "interval": interval,
    }


def _mark_provider_connected_after_oauth(provider: str) -> None:
    """A finished OAuth flow is an explicit operator connection. Record it so
    the fail-closed spend gate (provider_is_connected) goes green, exactly like
    the manual key-save path — otherwise the tokens land in the auth file but
    the provider keeps showing NOT CONNECTED / "env key only"."""
    try:
        from forven.model_selection import mark_provider_connected

        mark_provider_connected(provider)
    except Exception:
        log.exception("failed to mark %s connected after oauth", provider)


def _complete_openai_oauth(state: str, code: str, code_verifier: str | None) -> None:
    from forven.auth import openai as openai_auth

    if not state:
        raise HTTPException(status_code=400, detail="missing oauth state")
    if not code:
        raise HTTPException(status_code=400, detail="missing oauth code")

    session = _peek_oauth_session("openai", state)
    if not session:
        raise HTTPException(status_code=400, detail="oauth session expired or invalid")

    if not code_verifier:
        stored = session or {}
        code_verifier = str(stored.get("code_verifier") or "")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="missing code_verifier for openai oauth")

    normalized_code = _coerce_oauth_code(code)
    if not normalized_code:
        raise HTTPException(status_code=400, detail="missing oauth code")

    try:
        response = httpx.post(
            openai_auth.TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": openai_auth.CLIENT_ID,
                "code": normalized_code,
                "code_verifier": code_verifier,
                "redirect_uri": openai_auth.REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
    except Exception as exc:
        log.exception("openai oauth token endpoint unreachable")
        raise HTTPException(status_code=502, detail="unable to reach openai oauth token endpoint") from exc

    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"OpenAI token exchange failed: {_http_error_detail(response)}",
        ) from exc

    token_payload = response.json()

    access_token = str(token_payload.get("access_token") or "").strip()
    if not access_token:
        raise HTTPException(status_code=400, detail="OAuth response missing access token")
    expires = token_payload.get("expires_in", 86400)
    try:
        expires_ms = int(time.time() * 1000 + float(expires) * 1000)
    except Exception:
        expires_ms = int(time.time() * 1000 + 86400 * 1000)

    profile = {
        "type": "oauth",
        "provider": "openai",
        "access": access_token,
        "refresh": str(token_payload.get("refresh_token", "")).strip(),
        "expires": expires_ms,
    }

    # H-S5: validated extraction via safe helper (issuer + value checks)
    from forven.auth import safe_extract_chatgpt_account_id
    account_id = safe_extract_chatgpt_account_id(access_token)
    if account_id:
        profile["accountId"] = account_id

    upsert_profile("openai", profile)
    _mark_provider_connected_after_oauth("openai")
    _consume_oauth_session("openai", state)


def _complete_minimax_oauth(state: str) -> None:
    """Backward-compat: delegate to single-poll status.

    Returns silently on 'complete'; raises HTTPException for any other status
    so legacy callers (POST /oauth/complete) see an error rather than blocking.
    Frontends should drive cadence via /oauth/status instead.
    """
    status = get_auth_provider_oauth_status("minimax", state)
    s = status.get("status")
    if s == "complete":
        return
    if s in ("awaiting_user", "slow_down"):
        raise HTTPException(
            status_code=425,
            detail="oauth not yet complete; poll /oauth/status",
        )
    if s in ("expired", "denied"):
        raise HTTPException(status_code=400, detail=s)
    raise HTTPException(
        status_code=400,
        detail=str(status.get("error") or "oauth failed"),
    )


def _oauth_error_code(payload: object) -> str:
    if not isinstance(payload, dict):
        return "unknown_error"
    error_value = payload.get("error")
    if isinstance(error_value, dict):
        error = str(
            error_value.get("code")
            or error_value.get("error")
            or error_value.get("message")
            or "unknown_error"
        )
    elif isinstance(error_value, str):
        error = error_value
    else:
        error = str(
            payload.get("code")
            or payload.get("error_code")
            or payload.get("message")
            or payload.get("status")
            or "unknown_error"
        )
    return error.strip() or "unknown_error"


def _minimax_status_from_error(error: str, state: str, session: dict) -> dict | None:
    if error in ("authorization_pending", "pending", "not_authorized", "not_authorised"):
        return {"status": "awaiting_user"}
    if error == "slow_down":
        new_interval = min(int(session.get("interval", 2)) + 1, 10)
        session["interval"] = new_interval
        return {"status": "slow_down", "interval": new_interval}
    if error in ("expired_token", "expired"):
        _consume_oauth_session("minimax", state)
        return {"status": "expired"}
    if error in ("access_denied", "denied"):
        _consume_oauth_session("minimax", state)
        return {"status": "denied"}
    return None


def _extract_minimax_token_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        return {}

    candidates: list[dict[str, object]] = [payload]
    for key in ("data", "token", "result", "tokens"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)

    for candidate in candidates:
        access = (
            candidate.get("access_token")
            or candidate.get("accessToken")
            or candidate.get("access")
        )
        token_value = candidate.get("token")
        if not access and isinstance(token_value, str):
            access = token_value
        if access:
            normalized = dict(candidate)
            normalized["access_token"] = str(access).strip()
            refresh = normalized.get("refresh_token") or normalized.get("refreshToken")
            if refresh:
                normalized["refresh_token"] = str(refresh).strip()
            return normalized
    return {}


def _poll_minimax_once(state: str, session: dict) -> dict:
    """Perform ONE token-endpoint poll for an in-flight MiniMax device flow."""
    from forven.auth import minimax as minimax_auth

    try:
        attempt = httpx.post(
            minimax_auth.TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:user_code",
                "client_id": minimax_auth.CLIENT_ID,
                "user_code": str(session.get("user_code") or "").strip(),
                "code_verifier": str(session.get("code_verifier") or "").strip(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
            follow_redirects=True,
        )
    except Exception as exc:
        log.warning("minimax token poll network error: %s", exc)
        return {"status": "awaiting_user"}

    try:
        payload = attempt.json()
    except Exception:
        payload = {}

    if attempt.status_code == 200:
        token_payload = _extract_minimax_token_payload(payload)
        if token_payload.get("access_token"):
            expires = token_payload.get("expired_in") or token_payload.get("expiresAt")
            if not expires and token_payload.get("expires_in"):
                try:
                    expires = int(time.time() * 1000 + float(token_payload["expires_in"]) * 1000)
                except Exception:
                    expires = None
            elif isinstance(expires, str):
                try:
                    expires = int(expires)
                except Exception:
                    expires = None
            profile = {
                "type": "oauth",
                "provider": "minimax",
                "access": str(token_payload.get("access_token") or "").strip(),
                "refresh": str(token_payload.get("refresh_token", "")).strip(),
                "expires": int(expires) if isinstance(expires, (int, float)) else None,
            }
            upsert_profile("minimax", profile)
            _mark_provider_connected_after_oauth("minimax")
            _consume_oauth_session("minimax", state)
            return {"status": "complete"}
        status = _minimax_status_from_error(_oauth_error_code(payload), state, session)
        if status is not None:
            return status
        return {"status": "awaiting_user"}

    error = _oauth_error_code(payload)
    status = _minimax_status_from_error(error, state, session)
    if status is not None:
        return status
    return {"status": "error", "error": error}


def start_auth_provider_oauth(provider: str):
    normalized_provider = _normalize_auth_provider(provider)
    if normalized_provider == "openai":
        return _build_openai_oauth_start()
    if normalized_provider == "minimax":
        return _build_minimax_oauth_start()
    raise HTTPException(status_code=400, detail=f"unsupported oauth provider: {provider}")


def complete_auth_provider_oauth(provider: str, body: AuthProviderOAuthCompleteBody):
    normalized_provider = _normalize_auth_provider(provider)
    state = str(body.state or "").strip()
    code = str(body.code or "").strip()
    code_verifier = str(body.code_verifier or "").strip() or None

    if not state:
        raise HTTPException(status_code=400, detail="missing oauth state")

    if normalized_provider == "openai":
        _complete_openai_oauth(state, code, code_verifier)
    elif normalized_provider == "minimax":
        if not code:
            # Minimax uses the device/user-code flow and does not return an auth code.
            code = ""
        _complete_minimax_oauth(state)
    else:
        raise HTTPException(status_code=400, detail=f"unsupported oauth provider: {provider}")

    return {
        "ok": True,
        "provider": normalized_provider,
        "status": _build_auth_provider_payload(normalized_provider)["status"],
        "message": f"{normalized_provider} authentication configured",
    }


def get_auth_provider_oauth_status(provider: str, state: str) -> dict:
    """Return current status of an in-flight OAuth flow.

    Status enum: awaiting_user | code_received | complete | expired | denied
                 | slow_down | error
    """
    normalized_provider = _normalize_auth_provider(provider)
    if normalized_provider not in ("openai", "minimax"):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported oauth provider: {provider}",
        )

    result = _peek_oauth_result(normalized_provider, state)
    if result is not None:
        return result

    session = _peek_oauth_session(normalized_provider, state)
    if not session:
        return {"status": "expired"}

    if normalized_provider == "openai":
        if session.get("completion_started"):
            return {"status": "code_received"}
        listener = session.get("listener")
        if listener is None:
            return {"status": "awaiting_user"}
        if listener.expired():
            _consume_oauth_session("openai", state)
            try:
                listener.shutdown()
            except Exception:
                pass
            return {"status": "expired"}
        session_callback_code = str(session.get("callback_code") or "").strip()
        recorded_callback_code = _AUTH_OAUTH_CALLBACKS.get("openai", {}).get(state)
        captured_code = session_callback_code or recorded_callback_code or listener.code
        if not captured_code:
            return {"status": "awaiting_user"}
        listener_state = getattr(listener, "state", None)
        if captured_code == listener.code and listener_state and listener_state != state:
            return {"status": "awaiting_user"}
        verifier = str(session.get("code_verifier") or "")
        try:
            _complete_openai_oauth(state, captured_code, verifier)
        except HTTPException as exc:
            return {"status": "error", "error": str(exc.detail)}
        finally:
            try:
                listener.shutdown()
            except Exception:
                pass
        return {"status": "complete"}

    if normalized_provider == "minimax":
        return _poll_minimax_once(state, session)

    return {"status": "awaiting_user"}


def cancel_auth_provider_oauth(provider: str, state: str) -> dict:
    """Cancel an in-flight OAuth flow: release listener (if any) and clear session."""
    normalized_provider = _normalize_auth_provider(provider)
    session = _consume_oauth_session(normalized_provider, state)
    _AUTH_OAUTH_RESULTS.get(normalized_provider, {}).pop(state, None)
    _shutdown_session_listener(session)

    return {"ok": True, "provider": normalized_provider}


def _classify_activity_log_event(entry: dict) -> str | None:
    """Map activity_log rows to coarse websocket event names."""
    if not isinstance(entry, dict):
        return None

    source = str(entry.get("source") or "").strip().lower()
    level = str(entry.get("level") or "").strip().lower()
    msg = str(entry.get("message") or "").strip().lower()

    if "kill switch" in msg or (source == "daemon" and level == "critical"):
        return "kill_switch_activated"

    if source == "integrations" and "session" in msg and "opened" in msg:
        return "mcp_session_opened"

    if (
        "lifecycle transition" in msg
        or "pipeline override" in msg
        or "promoted" in msg
        or "promote" in msg
    ):
        return "strategy_promoted"

    if "stage transition" in msg or "transitioned" in msg:
        return "strategy_transition"

    if "queued execution task" in msg or "task queued" in msg or ("assign" in msg and "task" in msg):
        return "task_queued"

    if "task completed" in msg or "completed task" in msg:
        return "task_completed"

    if "task failed" in msg or "failed task" in msg:
        return "task_failed"

    if "task started" in msg or "started task" in msg:
        return "task_status_changed"

    if (
        "daily loss limit" in msg
        or "drawdown" in msg
        or "risk alert" in msg
        or source == "risk"
    ):
        return "risk_alert"

    return None


def _coalesce_ws_messages(messages: list[dict]) -> dict | None:
    payloads = [dict(message) for message in messages if isinstance(message, dict)]
    if not payloads:
        return None
    if len(payloads) == 1:
        return payloads[0]
    return {"type": "batch", "messages": payloads}


def _chroma_backtest_records():
    """Legacy secondary backtest-result source — permanently empty.

    The ChromaDB vector layer was removed 2026-07-02 (it had been disabled in
    production for months and held no records). SQLite backtest_results rows +
    artifacts are the canonical store; callers that still merge this source get
    an empty list. Kept as a stub so the merge sites need no rewrite — safe to
    inline away whenever those paths are next touched.
    """
    return []


def _resolve_backtest_results_remote_api() -> str | None:
    raw = str(os.getenv(_BACKTEST_RESULTS_REMOTE_API_ENV, "") or "").strip()
    if not raw:
        # Settings fallback for machine-local remote engine configuration.
        settings = _load_settings_payload()
        remote_enabled = _coerce_bool(settings.get("remote_engine_enabled"), False)
        remote_url = str(settings.get("remote_engine_url") or "").strip()
        if remote_enabled and remote_url:
            raw = remote_url
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = f"http://{raw}"
    raw = raw.rstrip("/")
    if not raw.endswith("/api"):
        raw = f"{raw}/api"
    return raw


def _is_remote_configured() -> bool:
    """True when the user has configured a remote results source via env."""
    return _resolve_backtest_results_remote_api() is not None


def _fetch_remote_backtest_summaries(
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 200,
    *,
    log_errors: bool = True,
) -> list[dict]:
    remote_api = _resolve_backtest_results_remote_api()
    if not remote_api:
        return []

    params: dict[str, str | int] = {
        "limit": max(1, int(limit)),
        "remote_skip": "1",
    }
    if strategy:
        params["strategy"] = strategy
    if symbol:
        params["symbol"] = symbol

    target = f"{remote_api}/results"
    try:
        resp = httpx.get(target, params=params, timeout=_BACKTEST_RESULTS_REMOTE_TIMEOUT_SECONDS)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        if log_errors:
            log.warning("Remote backtest results fetch failed (%s): %s", target, exc)
        return []

    rows: list[object]
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        candidate = payload.get("results")
        if not isinstance(candidate, list):
            candidate = payload.get("items")
        if not isinstance(candidate, list):
            candidate = payload.get("data")
        rows = candidate if isinstance(candidate, list) else []
    else:
        rows = []

    normalized: list[dict] = []
    for row in rows:
        parsed = _coerce_backtest_summary_payload(row)
        if parsed:
            normalized.append(parsed)
    normalized.sort(key=lambda r: _to_datetime_sort_key(r.get("created_at")), reverse=True)
    return normalized


def _fetch_remote_backtest_detail(result_id: str, *, log_errors: bool = True) -> dict | None:
    remote_api = _resolve_backtest_results_remote_api()
    if not remote_api:
        return None

    encoded_id = urllib.parse.quote(str(result_id).strip(), safe="")
    target = f"{remote_api}/results/{encoded_id}"
    try:
        resp = httpx.get(
            target,
            params={"remote_skip": "1"},
            timeout=_BACKTEST_RESULTS_REMOTE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if log_errors:
            log.warning("Remote backtest detail fetch failed (%s): %s", target, exc)
        return None

    if resp.status_code == 404:
        return None

    try:
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        if log_errors:
            log.warning("Remote backtest detail decode failed (%s): %s", target, exc)
        return None

    return payload if isinstance(payload, dict) else None


def _is_remote_backtest_results_available() -> bool:
    remote_api = _resolve_backtest_results_remote_api()
    if not remote_api:
        return False
    try:
        resp = httpx.get(
            f"{remote_api}/results",
            params={"limit": 1, "remote_skip": "1"},
            timeout=min(_BACKTEST_RESULTS_REMOTE_TIMEOUT_SECONDS, 3.0),
        )
        if resp.status_code >= 500:
            return False
        if resp.status_code == 404:
            return False
        return True
    except Exception:
        return False


def _resolve_remote_backtesting_mode() -> tuple[bool, str | None]:
    """Compatibility helper for call sites expecting (enabled, api_base)."""
    api_base = _resolve_backtest_results_remote_api()
    return (api_base is not None, api_base)


def _fetch_remote_backtest_results(
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Compatibility wrapper over current remote summary fetcher."""
    return _fetch_remote_backtest_summaries(
        strategy=strategy,
        symbol=symbol,
        limit=limit,
        log_errors=True,
    )


def _fetch_remote_backtest_result(result_id: str) -> dict | None:
    """Compatibility wrapper over current remote detail fetcher."""
    return _fetch_remote_backtest_detail(result_id, log_errors=True)


def _is_remote_backtesting_reachable(api_base: str) -> bool:
    """Health-check a remote backtesting API base URL."""
    origin = str(api_base or "").rstrip("/")
    if not origin:
        return False
    if origin.endswith("/api"):
        origin = origin[:-4]
    for path in ("/health", "/api/health"):
        try:
            response = httpx.get(
                f"{origin}{path}",
                params={"remote_skip": "1"},
                timeout=1.5,
            )
            if response.status_code < 500 and response.status_code != 404:
                return True
        except Exception:
            continue
    return False


# â”€â”€ Existing endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def post_brain_chat(body: BrainChatBody):
    message = str(body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    payload: dict[str, object] = {
        "kind": "brain_invoke",
        "message": message,
        "source": "ui_chat",
    }

    context = str(body.context or "").strip()
    if context:
        payload["context"] = context

    entity_type = str(body.entity_type or "").strip().lower()
    if entity_type:
        payload["entity_type"] = entity_type
    entity_id = str(body.entity_id or "").strip()
    if entity_id:
        payload["entity_id"] = entity_id

    provider = str(body.provider or "").strip()
    if provider:
        payload["provider"] = provider

    model = str(body.model or "").strip()
    if model:
        payload["model"] = model

    if body.history:
        payload["history"] = [{"role": h.role, "content": h.content} for h in body.history]

    with get_db() as conn:
        task_id = create_pending_task(
            conn,
            "brain_invoke",
            payload,
            priority=1,
            source="user",
        )

    if task_id <= 0:
        raise HTTPException(status_code=500, detail="failed to queue brain task")

    return {"ok": True, "task_id": task_id}


async def post_brain_chat_direct(body: BrainChatBody):
    """Synchronous chat — returns the assistant response directly, no task queue."""
    message = str(body.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")

    from forven.agents.runner import (
        AGENT_TOOLS,
        BACKTESTING_TOOLS,
        BRAIN_TOOLS,
        _call_with_tools,
    )
    from forven.agents.tool_definitions import CHAT_ASK_TOOL_NAMES
    from forven.brain import resolve_brain_provider_model
    from forven.context import build_chat_context

    provider, model = resolve_brain_provider_model(
        str(body.provider or "").strip() or None,
        str(body.model or "").strip() or None,
    )

    # Chat mode is read-only-but-grounded: give the Brain the curated read-only
    # tool set so it can answer from LIVE data (e.g. "how is S00719 doing?")
    # while remaining unable to mutate state. Single source of truth lives in
    # tool_definitions.CHAT_ASK_TOOL_NAMES.
    chat_tools = [
        tool
        for tool in list(AGENT_TOOLS) + list(BRAIN_TOOLS) + list(BACKTESTING_TOOLS)
        if tool["name"] in CHAT_ASK_TOOL_NAMES
    ]

    context = build_chat_context()
    if chat_tools:
        context += (
            "\n\n---\n\n# TOOLS\n"
            "You have read-only tools to look things up (strategy code, datasets, "
            "backtest results, memory). Use them to ground your answers in live data "
            "instead of guessing. You cannot change anything from here."
        )
    ui_path = str(body.context or "").strip()
    entity_type = str(body.entity_type or "").strip().lower()
    entity_id = str(body.entity_id or "").strip()
    if entity_type and entity_id:
        context += (
            "\n\n---\n\n# USER CONTEXT\n"
            f"The user is currently viewing {entity_type} **{entity_id}**"
            f"{f' (path: {ui_path})' if ui_path else ''}.\n"
            "When the user refers to 'this' / 'it' / 'the current one', assume they mean this entity unless they say otherwise."
        )
    elif ui_path:
        context += f"\n\n---\n\n# USER CONTEXT\nThe user is on page: {ui_path}"

    messages: list[dict[str, str]] = []
    if body.history:
        for entry in body.history[-20:]:
            role = str(getattr(entry, "role", "") or "").strip()
            content = str(getattr(entry, "content", "") or "").strip()
            if role in {"user", "assistant"} and content:
                messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": message})

    try:
        result = await _call_with_tools(provider, model, messages, context, tools=chat_tools or None)
    except Exception as exc:
        from forven.ai import _is_rate_limit_exception

        if _is_rate_limit_exception(exc):
            log.warning("Direct brain chat rate limited: %s", exc)
            return {
                "ok": False,
                "error": (
                    f"{provider or 'The configured provider'} is rate limiting this key right now. "
                    "Wait a minute and try again, or switch Brain to another provider/model in Settings."
                ),
                "error_code": "provider_rate_limited",
                "retryable": True,
                "mode": "direct",
            }
        # Surface a missing-credentials error as actionable config, not a raw stack class.
        message = str(exc)
        if "no api credentials" in message.lower() or "no auth profile" in message.lower():
            log.warning("Direct brain chat: provider unconfigured: %s", exc)
            return {
                "ok": False,
                "error": message,
                "error_code": "provider_unconfigured",
                "retryable": False,
                "mode": "direct",
            }
        log.exception("Direct brain chat failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "mode": "direct"}

    if isinstance(result, tuple) and result:
        response_text = str(result[0])
    else:
        response_text = str(result)

    return {"ok": True, "response": response_text, "mode": "direct"}


def get_brain_chat_result(task_id: int):
    if task_id <= 0:
        raise HTTPException(status_code=400, detail="invalid task id")

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, status, result, error, created_at, completed_at "
            "FROM tasks WHERE id = ? AND type = 'brain_invoke' LIMIT 1",
            (task_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")

    item = dict(row)
    result_payload = _safe_json(item.get("result"))
    if result_payload is None and item.get("result"):
        result_payload = {"response": str(item.get("result"))}

    return {
        "ok": True,
        "status": str(item.get("status") or "pending").lower(),
        "result": result_payload,
        "error": item.get("error"),
        "created_at": item.get("created_at"),
        "completed_at": item.get("completed_at"),
    }


def get_pipeline_settings():
    return _load_pipeline_settings_payload()


def _find_null_setting_leaves(value, prefix: str = "") -> list[str]:
    """Return dotted paths of every ``None`` leaf in a (possibly nested) update."""
    paths: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            nested_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_find_null_setting_leaves(nested, nested_prefix))
    elif value is None:
        paths.append(prefix or "<root>")
    return paths


def put_pipeline_settings(body: PipelineSettingsUpdateBody):
    updates = body.updates or {}
    if not isinstance(updates, dict):
        raise HTTPException(status_code=400, detail="updates must be an object")
    # Refuse empty values OUTRIGHT rather than persisting them: a null that
    # reaches the promotion-gate config (e.g. gauntlet.min_trades) survives the
    # raw merge below and later crashes every gate evaluation with
    # float(None)/int(None). No pipeline setting legitimately accepts null, so
    # an empty field is always an operator slip — name it and reject the save.
    null_paths = _find_null_setting_leaves(updates)
    if null_paths:
        raise HTTPException(
            status_code=400,
            detail=(
                "Empty value for setting(s): "
                + ", ".join(sorted(null_paths))
                + " — enter a value or revert the field before saving"
            ),
        )
    # Serialize the whole load->merge->save against every other settings
    # mutation so a concurrent pipeline (or section) save can't race the
    # read-modify-write and lose an edit.
    with _SETTINGS_MUTATION_LOCK:
        payload = _load_pipeline_settings_payload()
        threshold_updates = {key: value for key, value in updates.items() if key in _PIPELINE_THRESHOLD_SETTING_KEYS}
        flat_updates = {key: value for key, value in updates.items() if key not in _PIPELINE_THRESHOLD_SETTING_KEYS}
        if threshold_updates:
            from forven.policy import load_pipeline_config, save_pipeline_config

            policy_config = load_pipeline_config()
            for key, value in threshold_updates.items():
                if isinstance(value, dict) and isinstance(policy_config.get(key), dict):
                    policy_config[key] = {**policy_config[key], **value}
                else:
                    policy_config[key] = value
            # forven:pipeline_thresholds lives in policy.py and opens its own
            # write; it stays a distinct KV key from the pipeline payload +
            # WIP-cap mirror. The lock serializes it against the payload write
            # below; a crash between them leaves the thresholds updated but the
            # display payload stale (never a torn payload+mirror).
            save_pipeline_config(policy_config)
        payload.update(flat_updates)
        _normalize_pipeline_wip_cap_payload(payload)
        _normalize_graveyard_strategy_limit_payload(payload)
        payload["created_by"] = str(body.actor or "manual") or "manual"
        payload["created_at"] = _now()
        # Write the pipeline payload AND its WIP-cap KV mirror in ONE
        # transaction so the display blob and the enforced per-stage caps can
        # never diverge on a crash between them.
        kv_set_many({
            _SETTINGS_PIPELINE_STORAGE_KEY: payload,
            **_pipeline_wip_cap_kv_items(payload),
        })
    return payload


_PIPELINE_THRESHOLD_SETTING_KEYS = {
    # Active stance preset (relaxed | default | strict | custom). A plain string,
    # not a section dict — routes to the pipeline KV so policy._apply_pipeline_preset
    # can resolve the bundle on load.
    "pipeline_preset",
    "testing_mode",
    "quick_screen",
    "gauntlet",
    "walk_forward",
    "robustness_thresholds",
    "paper_trading",
    # Absolute anti-bypass floors (incl. the real-money live_* rails). Must route to
    # the pipeline KV that the promotion gates read, not the flat settings blob.
    "safety_floors",
    "live_graduated",
    "paper_gate",
    "deploy_gate",
    "retirement",
    "decay",
}

# Keys that exist in BOTH the main settings blob and the flat pipeline payload
# with DIFFERENT meanings. ``max_drawdown_pct`` is the risk kill-switch in the
# blob (default 30, enforced by forven.exchange.risk reading the blob directly)
# and a legacy promotion threshold in the pipeline payload (default 40). The
# pipeline overlay below must never shadow the blob value, otherwise the
# Trading > Risk field displays the un-enforced pipeline number and edits
# (which correctly write the blob) appear not to stick. The pipeline twin stays
# reachable via GET /api/settings/pipeline for its own consumers.
_PIPELINE_OVERLAY_SHADOWED_KEYS = frozenset({"max_drawdown_pct"})


def get_settings():
    payload = _load_settings_payload()
    try:
        pipeline_settings = {
            key: value
            for key, value in _load_pipeline_settings_payload().items()
            if key not in _PIPELINE_THRESHOLD_SETTING_KEYS
            and key not in _PIPELINE_OVERLAY_SHADOWED_KEYS
        }
        payload.update(pipeline_settings)
    except Exception:
        pass
    try:
        from forven.policy import load_pipeline_config, pipeline_thresholds_for_display

        # The settings UI presents ratio thresholds with a "%" unit, so convert
        # the canonical fractions (0.30) to whole percent (30) for display.
        policy_config = pipeline_thresholds_for_display(load_pipeline_config())
        for key in _PIPELINE_THRESHOLD_SETTING_KEYS:
            if key in policy_config:
                payload[key] = policy_config[key]
    except Exception:
        pass
    # Resolved preset bundles (same display units as the threshold keys above) so the
    # Settings UI can fill every gate knob live when the operator picks a stance —
    # without the frontend hardcoding (and drifting from) the policy preset values.
    try:
        from forven.policy import _normalize_pipeline_config, pipeline_thresholds_for_display

        payload["pipeline_presets"] = {
            _name: pipeline_thresholds_for_display(_normalize_pipeline_config({"pipeline_preset": _name}))
            for _name in ("relaxed", "default", "strict")
        }
    except Exception:
        pass
    # Throughput preset bundles + the DERIVED active name (value-compare; nothing
    # named is persisted). The backend owns both so the Settings dial, the API
    # payload, and telemetry can never disagree about which preset is in effect.
    try:
        from forven.throughput_policy import THROUGHPUT_PRESETS, effective_throughput_preset

        payload["throughput_presets"] = {
            _name: dict(_bundle) for _name, _bundle in THROUGHPUT_PRESETS.items()
        }
        payload["throughput_preset_effective"] = effective_throughput_preset(payload)
    except Exception:
        pass
    # Reflect the authoritative regime-gating values (config.json + env overrides),
    # which the live gate actually enforces, rather than the stale KV blob — so the
    # Lab/Risk panel can't show a value diverging from what's enforced.
    try:
        from forven import config as _regime_cfg
        payload["strict_regime_gating"] = _regime_cfg.get_strict_regime_gating()
        payload["regime_min_confidence"] = _regime_cfg.get_regime_min_confidence()
        payload["allow_unknown_regime_strategies"] = _regime_cfg.get_allow_unknown_regime_strategies()
        payload["regime_gate_mode"] = _regime_cfg.get_regime_gate_mode()
        payload["regime_gate_block_long"] = ",".join(sorted(_regime_cfg.get_regime_gate_block_long()))
        payload["regime_gate_block_short"] = ",".join(sorted(_regime_cfg.get_regime_gate_block_short()))
        payload["regime_gate_min_confidence"] = _regime_cfg.get_regime_gate_min_confidence()
    except Exception:
        pass
    # Reflect the REAL Discord delivery preferences so the Notifications panel
    # shows authoritative state (the toggles bridge into this store on save). A
    # toggle is "on" only if every pref it drives is on, via the same mapping the
    # write path uses — so an out-of-band divergence surfaces instead of lying.
    try:
        from forven.notifications import get_notification_preferences

        _prefs = get_notification_preferences()
        payload["notification_level"] = (
            "none" if str(_prefs.get("discord_mode") or "policy").strip().lower() == "shadow" else "all"
        )
        for _toggle, _pref_keys in _NOTIF_TOGGLE_PREF_KEYS.items():
            payload[_toggle] = all(bool(_prefs.get(_pk, True)) for _pk in _pref_keys)
    except Exception:
        pass
    # OPS-4: the real-money arming flag is an ENV var, not a stored setting, so it
    # never appeared next to the trading-mode controls it silently overrules. A
    # live-armed instance read identically to a testnet-only one.
    _arming = mainnet_arming_snapshot()
    payload["mainnet_armed"] = bool(_arming.get("armed"))
    payload["mainnet_arming"] = _arming
    return payload


def get_settings_audit_log(limit: int = 5) -> list[dict]:
    """Return the most recent audit entries, newest first.

    limit=0 or negative returns the full log (up to the 50-entry cap).
    """
    payload = _load_settings_payload()
    log = payload.get("audit_log") or []
    reversed_log = list(reversed(log))
    if limit and limit > 0:
        return reversed_log[:limit]
    return reversed_log


def put_settings_section(section: str, payload: dict):
    # Serialize the entire read->apply->diff->audit->save sequence so two
    # concurrent section saves can't race the read-modify-write (losing one
    # edit) or cross-attribute the audit diff. _apply_settings_section computes
    # the audit entries against its own pre-mutation snapshot and persists the
    # secrets + main blob (audit included) in ONE atomic transaction, so there
    # is no longer a second re-read/diff/save out here.
    with _SETTINGS_MUTATION_LOCK:
        return _apply_settings_section(section, payload, actor="ui")


def get_settings_discord_audit(send_probe: bool = False):
    try:
        from forven.bot import run_discord_audit

        return run_discord_audit(send_probe=send_probe)
    except Exception as exc:
        log.exception("Discord audit failed")
        raise HTTPException(status_code=500, detail=f"Discord audit failed: {exc}") from exc


def post_settings_test_discord():
    audit = get_settings_discord_audit(send_probe=True)
    summary = audit.get("summary") if isinstance(audit, dict) else {}
    failed = int((summary or {}).get("failed", 0) or 0)
    if failed > 0:
        failures = (summary or {}).get("failures", []) or []
        first = failures[0] if failures else {}
        actor = str(first.get("actor") or "unknown")
        alias = str(first.get("channel_alias") or "unknown")
        detail = str(first.get("detail") or first.get("status") or "unknown error")
        raise HTTPException(
            status_code=400,
            detail=f"Discord audit failed for {actor} -> #{alias}: {detail}",
        )
    return {"status": "ok", "source": "discord", "tested_at": _now(), "audit": audit}


def post_settings_reset():
    _save_settings_payload(_default_settings_payload())
    return {"status": "ok"}


def post_settings_test_remote_engine(body: SettingsTestRemoteEngineBody):
    import httpx
    url = str(body.url or "").strip()
    if not url:
         return {"ok": False, "message": "URL is empty"}
         
    try:
         target = f"{url.rstrip('/')}/health"
         response = httpx.get(target, timeout=5.0)
         if response.status_code == 200:
             return {"ok": True, "message": f"Successfully connected to {url}", "data": response.json()}
         return {"ok": False, "message": f"Server returned status {response.status_code}"}
    except Exception:
         return {"ok": False, "message": "Connection failed. Make sure the server is running and the IP/Port is correct."}


def get_settings_api_keys():
    store = _load_api_keys_payload()
    keys: list[dict] = []
    for source in _DEFAULT_API_KEY_SOURCES:
        entry = store.get(source, {})
        if isinstance(entry, dict):
            value = str(entry.get("value", "")).strip()
            last_tested = entry.get("last_tested")
            test_status = entry.get("test_status")
        else:
            value = str(entry or "").strip()
            last_tested = None
            test_status = None
        keys.append({
            "source": source,
            "is_configured": bool(value),
            "last_tested": last_tested,
            "test_status": test_status,
        })
    for source, entry in store.items():
        if source in _DEFAULT_API_KEY_SOURCES:
            continue
        if isinstance(entry, dict):
            value = str(entry.get("value", "")).strip()
            last_tested = entry.get("last_tested")
            test_status = entry.get("test_status")
        else:
            value = str(entry or "").strip()
            last_tested = None
            test_status = None
        keys.append({
            "source": source,
            "is_configured": bool(value),
            "last_tested": last_tested,
            "test_status": test_status,
        })
    return keys


def post_settings_api_key(body: SettingsApiKeyBody):
    source = _normalize_api_key_source(body.source)
    api_key = str(body.api_key or "").strip()
    if not source or not api_key:
        raise HTTPException(status_code=400, detail="source and api_key are required")

    store = _load_api_keys_payload()
    record = store.get(source) if isinstance(store.get(source), dict) else {}
    record = record if isinstance(record, dict) else {}
    record.update({
        "value": api_key,
        "last_tested": None,
        "test_status": None,
    })
    store[source] = record
    _save_api_keys_payload(store)
    return {"status": "ok", "source": source}


def delete_settings_api_key(source: str):
    source = _normalize_api_key_source(source)
    store = _load_api_keys_payload()
    if source in store:
        store.pop(source)
        _save_api_keys_payload(store)
    return {"status": "ok", "source": source}


def test_settings_api_key(source: str):
    source = _normalize_api_key_source(source)
    store = _load_api_keys_payload()
    entry = store.get(source)
    if not isinstance(entry, dict):
        raise HTTPException(status_code=404, detail=f"API key for {source} not configured")

    value = str(entry.get("value", "")).strip()
    if not value:
        raise HTTPException(status_code=404, detail=f"API key for {source} not configured")

    tested_at = _now()

    # Actually validate Polygon keys against the API
    if source == "polygon":
        try:
            from forven.polygon_client import PolygonClient
            client = PolygonClient(api_key=value)
            try:
                valid = client.validate_key()
            finally:
                client.close()
            entry["test_status"] = "success" if valid else "failed"
        except Exception as exc:
            entry["test_status"] = "failed"
            entry["test_error"] = str(exc)
    else:
        entry["test_status"] = "success"

    entry["last_tested"] = tested_at
    store[source] = entry
    _save_api_keys_payload(store)

    return {"status": "ok", "source": source, "tested_at": tested_at, "test_status": entry["test_status"]}


def get_pipeline_config():
    """Load current pipeline thresholds from policy module."""
    from forven.policy import load_pipeline_config
    return load_pipeline_config()

def update_pipeline_config(config: dict):
    """Save pipeline thresholds using policy module."""
    from forven.policy import save_pipeline_config
    save_pipeline_config(config)
    return {"ok": True}


_PIPELINE_STAGE_ORDER = {
    "quick_screen": 1,
    "gauntlet": 2,
    "paper": 3,
    "live_graduated": 4,
}
_LIVE_TRADING_STAGES = {"paper", "live_graduated"}
_MOTION_DECISION_METRIC_KEYS = (
    "total_trades",
    "paper_trades",
    "sharpe",
    "sharpe_ratio",
    "live_sharpe",
    "live_sharpe_72h",
    "baseline_sharpe",
    "profit_factor",
    "max_drawdown_pct",
    "max_drawdown",
    "win_rate",
    "fitness",
    "degradation",
    "trade_count_72h",
    "min_trades",
    "min_paper_trades",
)


def _normalize_pipeline_stage(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = _to_core_status(raw) or _normalize_status(raw)
    return normalized if normalized else None


def _classify_pipeline_motion_type(from_state: str | None, to_state: str | None) -> str:
    normalized_from = _normalize_pipeline_stage(from_state)
    normalized_to = _normalize_pipeline_stage(to_state)
    if normalized_from == normalized_to:
        return "no_change"

    from_rank = _PIPELINE_STAGE_ORDER.get(str(normalized_from or ""))
    to_rank = _PIPELINE_STAGE_ORDER.get(str(normalized_to or ""))

    if from_rank is not None and to_rank is not None:
        return "promotion" if to_rank > from_rank else "demotion"
    if from_rank is None and to_rank is not None:
        return "promotion"
    if from_rank is not None and to_rank is None:
        return "demotion"

    if normalized_to in {"archived", "rejected"} and normalized_from:
        return "demotion"
    if normalized_from in {"archived", "rejected"} and normalized_to:
        return "promotion"

    if normalized_to in _LIVE_TRADING_STAGES and normalized_from not in _LIVE_TRADING_STAGES:
        return "promotion"
    if normalized_from in _LIVE_TRADING_STAGES and normalized_to not in _LIVE_TRADING_STAGES:
        return "demotion"
    return "transition"


def _motion_pipeline_memberships(from_state: str | None, to_state: str | None) -> list[str]:
    memberships: list[str] = []
    normalized_from = _normalize_pipeline_stage(from_state)
    normalized_to = _normalize_pipeline_stage(to_state)
    states = {state for state in (normalized_from, normalized_to) if state}

    if states & set(_PIPELINE_STAGE_ORDER.keys()):
        if states & {"quick_screen", "gauntlet"}:
            memberships.append("pipeline")
        if states & _LIVE_TRADING_STAGES:
            memberships.append("live_trading")
        if not memberships:
            memberships.append("pipeline")
    elif states:
        memberships.append("pipeline")
    return memberships


def _extract_strategy_ids_from_object(value: object, ids: set[str], depth: int = 0) -> None:
    if depth > 4:
        return
    if isinstance(value, dict):
        for key in ("strategy_id", "strategy", "lifecycle_strategy_id"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                ids.add(candidate.strip())
        for nested in value.values():
            _extract_strategy_ids_from_object(nested, ids, depth + 1)
        return
    if isinstance(value, list):
        for nested in value[:50]:
            _extract_strategy_ids_from_object(nested, ids, depth + 1)
        return
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        for match in re.findall(r"\bS\d{3,8}\b", text, flags=re.IGNORECASE):
            ids.add(match.strip())


def _extract_activity_strategy_ids(message: str, data: object) -> set[str]:
    ids: set[str] = set()
    _extract_strategy_ids_from_object(data, ids, depth=0)
    _extract_strategy_ids_from_object(message, ids, depth=0)
    return {value for value in ids if value}


def _summarize_strategy_metrics_for_motion(metrics_raw: object) -> dict:
    parsed = metrics_raw
    if isinstance(metrics_raw, str):
        parsed = _safe_json(metrics_raw)
    if not isinstance(parsed, dict):
        return {}

    target = parsed
    if isinstance(target.get("out_of_sample"), dict):
        target = target["out_of_sample"]
    if isinstance(target.get("metrics"), dict):
        target = target["metrics"]

    summary: dict[str, object] = {}
    for key in _MOTION_DECISION_METRIC_KEYS:
        if key in target:
            summary[key] = target.get(key)
    return summary


def _collect_motion_decision_metrics(details: object, related_activity: list[dict], snapshot: dict) -> dict:
    metrics: dict[str, object] = {}

    def _collect(source: object) -> None:
        if not isinstance(source, dict):
            return
        for key in _MOTION_DECISION_METRIC_KEYS:
            if key in source and key not in metrics:
                metrics[key] = source.get(key)
        for nested in source.values():
            if isinstance(nested, dict):
                for key in _MOTION_DECISION_METRIC_KEYS:
                    if key in nested and key not in metrics:
                        metrics[key] = nested.get(key)

    if isinstance(details, dict):
        _collect(details)
    for activity in related_activity:
        _collect(activity.get("data"))
    _collect(snapshot)
    return metrics


def _infer_motion_decision_mode(
    actor: object,
    reason: object,
    motion_type: str,
    related_activity: list[dict],
) -> str:
    actor_text = str(actor or "").strip().lower()
    reason_text = str(reason or "").strip().lower()
    activity_text = " ".join(
        str(item.get("message") or "").strip().lower()
        for item in related_activity
    )

    if "gate failure" in reason_text or ("gate" in reason_text and "reject" in reason_text):
        return "gate_rejected"
    if "manual pipeline override" in reason_text or "manual override" in reason_text:
        return "manual_override"
    if "manual pipeline override" in activity_text:
        return "manual_override"
    if "gate" in reason_text and ("passed" in reason_text or "allow" in reason_text):
        return "gate_passed"
    if motion_type == "demotion" and (
        actor_text == "decay_tracker" or ("decay" in activity_text and "demot" in activity_text)
    ):
        return "decay_auto_demotion"
    if motion_type == "promotion":
        return "promotion"
    if motion_type == "demotion":
        return "demotion"
    return "transition"


def _build_motion_decision_summary(
    *,
    strategy_id: str,
    from_state: str | None,
    to_state: str | None,
    motion_type: str,
    memberships: list[str],
    actor: object,
    reason: object,
    decision_mode: str,
) -> str:
    scope = "/".join(memberships) if memberships else "pipeline"
    parts = [
        f"{motion_type}: {from_state or '--'} -> {to_state or '--'}",
        f"scope={scope}",
        f"decision={decision_mode}",
        f"strategy={strategy_id}",
    ]
    actor_text = str(actor or "").strip()
    if actor_text:
        parts.append(f"actor={actor_text}")
    reason_text = str(reason or "").strip()
    if reason_text:
        parts.append(f"reason={reason_text}")
    return " | ".join(parts)


def _motion_metric_float(metrics: dict, *keys: str) -> float | None:
    for key in keys:
        if key not in metrics:
            continue
        value = metrics.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except Exception:
            continue
    return None


def _clean_layman_reason_text(value: object, max_len: int = 180) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return f"{text[:max_len - 3]}..."


def _build_motion_layman_reason(
    *,
    motion_type: str,
    decision_mode: str,
    from_state: str | None,
    to_state: str | None,
    reason: object,
    actor: object,
    metrics: dict,
) -> str:
    reason_text = _clean_layman_reason_text(reason)
    actor_text = str(actor or "").strip()
    motion_label = "Promoted" if motion_type == "promotion" else ("Demoted" if motion_type == "demotion" else "Moved")

    if decision_mode == "decay_auto_demotion":
        baseline = _motion_metric_float(metrics, "baseline_sharpe")
        live = _motion_metric_float(metrics, "live_sharpe_72h", "live_sharpe")
        degradation = _motion_metric_float(metrics, "degradation")
        if baseline is not None and live is not None:
            return (
                f"Demoted because live performance dropped: live Sharpe {live:.2f} "
                f"vs baseline Sharpe {baseline:.2f}."
            )
        if degradation is not None:
            return f"Demoted because live performance decayed by about {degradation * 100:.0f}%."
        return "Demoted because live performance decayed below the safety threshold."

    if decision_mode == "gate_passed":
        sharpe = _motion_metric_float(metrics, "sharpe", "sharpe_ratio")
        trades = _motion_metric_float(metrics, "paper_trades", "total_trades", "trade_count_72h")
        detail_parts: list[str] = []
        if sharpe is not None:
            detail_parts.append(f"Sharpe {sharpe:.2f}")
        if trades is not None:
            detail_parts.append(f"{int(round(trades))} trades")
        if detail_parts:
            return f"Promoted after passing gate checks ({', '.join(detail_parts)})."
        return "Promoted after passing all required gate checks."

    if decision_mode == "gate_rejected":
        if reason_text:
            return f"{motion_label} because it failed a gate check: {reason_text}"
        return f"{motion_label} because it failed a required gate check."

    if decision_mode == "manual_override":
        if reason_text:
            return f"{motion_label} by manual override: {reason_text}"
        return f"{motion_label} by manual override."

    if reason_text:
        if motion_type == "promotion":
            return f"Promoted because {reason_text.lower()}"
        if motion_type == "demotion":
            return f"Demoted because {reason_text.lower()}"
        return f"Moved from {from_state or '--'} to {to_state or '--'} because {reason_text.lower()}"

    if motion_type == "promotion":
        return "Promoted after meeting current performance and policy requirements."
    if motion_type == "demotion":
        if actor_text:
            return f"Demoted by {actor_text} due to policy/performance safeguards."
        return "Demoted due to policy/performance safeguards."
    return f"Moved from {from_state or '--'} to {to_state or '--'} by policy decision."


def get_pipeline_motion_log(limit: int = 200):
    """Combined pipeline/live-trading promotion-demotion decision log."""
    normalized_limit = max(1, min(int(limit or 200), 1000))
    event_fetch_limit = min(max(normalized_limit * 6, 300), 5000)
    activity_fetch_limit = min(max(normalized_limit * 10, 600), 10000)

    with get_db() as conn:
        event_rows = conn.execute(
            """
            SELECT
                e.*,
                s.display_id AS strategy_display_id,
                s.name AS strategy_name,
                s.stage AS strategy_stage,
                s.owner AS strategy_owner,
                s.metrics AS strategy_metrics
            FROM strategy_events e
            LEFT JOIN strategies s ON s.id = e.strategy_id
            ORDER BY e.created_at DESC
            LIMIT ?
            """,
            (event_fetch_limit,),
        ).fetchall()
        activity_rows = conn.execute(
            "SELECT level, source, message, data, created_at "
            "FROM activity_log ORDER BY created_at DESC LIMIT ?",
            (activity_fetch_limit,),
        ).fetchall()

    activity_by_strategy: dict[str, list[dict]] = {}
    for row in activity_rows:
        source = str(row["source"] or "").strip()
        message = str(row["message"] or "").strip()
        normalized = {
            "level": str(row["level"] or "").strip() or None,
            "source": source or None,
            "message": message or None,
            "data": _safe_json(row["data"]),
            "timestamp": row["created_at"],
        }
        strategy_ids = _extract_activity_strategy_ids(message=message, data=normalized.get("data"))
        for strategy_id in strategy_ids:
            activity_by_strategy.setdefault(strategy_id.lower(), []).append(normalized)

    payload: list[dict[str, object]] = []
    for raw_row in event_rows:
        row = dict(raw_row)
        strategy_id = str(row.get("strategy_id") or "").strip()
        if not strategy_id:
            continue

        normalized_from = _normalize_pipeline_stage(row.get("from_state"))
        normalized_to = _normalize_pipeline_stage(row.get("to_state"))
        motion_type = _classify_pipeline_motion_type(normalized_from, normalized_to)
        if motion_type not in {"promotion", "demotion"}:
            continue
        # Only include motions that enter/exit paper or live trading states.
        if not ({normalized_from, normalized_to} & _LIVE_TRADING_STAGES):
            continue

        memberships = _motion_pipeline_memberships(normalized_from, normalized_to)
        event_ts = _to_datetime_sort_key(row.get("created_at"))
        related_activity: list[dict] = []
        for activity in activity_by_strategy.get(strategy_id.lower(), []):
            if len(related_activity) >= 6:
                break
            message = str(activity.get("message") or "").strip().lower()
            delta_seconds = abs(_to_datetime_sort_key(activity.get("timestamp")) - event_ts)
            if delta_seconds > 6 * 3600 and not (
                "pipeline" in message
                or "promot" in message
                or "demot" in message
                or "transition" in message
                or "decay" in message
                or "gate" in message
            ):
                continue
            related_activity.append(dict(activity))

        details = _safe_json(row.get("details_json"))
        details_payload: dict | list | str | None
        if isinstance(details, (dict, list)):
            details_payload = details
        else:
            details_text = str(row.get("details_json") or "").strip()
            details_payload = details_text or None

        strategy_snapshot = {
            "current_state": _normalize_pipeline_stage(row.get("strategy_stage")),
            "current_owner": str(row.get("strategy_owner") or "").strip() or None,
            "metrics": _summarize_strategy_metrics_for_motion(row.get("strategy_metrics")),
        }
        decision_metrics = _collect_motion_decision_metrics(
            details=details if isinstance(details, dict) else {},
            related_activity=related_activity,
            snapshot=strategy_snapshot.get("metrics") or {},
        )
        decision_mode = _infer_motion_decision_mode(
            actor=row.get("actor"),
            reason=row.get("reason"),
            motion_type=motion_type,
            related_activity=related_activity,
        )
        decision_summary = _build_motion_decision_summary(
            strategy_id=strategy_id,
            from_state=normalized_from,
            to_state=normalized_to,
            motion_type=motion_type,
            memberships=memberships,
            actor=row.get("actor"),
            reason=row.get("reason"),
            decision_mode=decision_mode,
        )
        layman_reason = _build_motion_layman_reason(
            motion_type=motion_type,
            decision_mode=decision_mode,
            from_state=normalized_from,
            to_state=normalized_to,
            reason=row.get("reason"),
            actor=row.get("actor"),
            metrics=decision_metrics,
        )

        payload.append(
            {
                "event_id": int(row.get("id") or 0),
                "timestamp": row.get("created_at"),
                "strategy_id": strategy_id,
                "strategy_display_id": str(row.get("strategy_display_id") or "").strip() or None,
                "strategy_name": str(row.get("strategy_name") or "").strip() or None,
                "from_state": normalized_from,
                "to_state": normalized_to,
                "motion_type": motion_type,
                "pipelines": memberships,
                "actor": str(row.get("actor") or "").strip() or None,
                "owner_from": str(row.get("owner_from") or "").strip() or None,
                "owner_to": str(row.get("owner_to") or "").strip() or None,
                "reason": str(row.get("reason") or "").strip() or None,
                "layman_reason": layman_reason,
                "decision_mode": decision_mode,
                "decision_summary": decision_summary,
                "decision_metrics": decision_metrics,
                "details": details_payload,
                "strategy_snapshot": strategy_snapshot,
                "related_activity": related_activity,
            }
        )
        if len(payload) >= normalized_limit:
            break

    return payload


def _normalize_ratio_metric(value: object) -> float | None:
    parsed = _coerce_optional_float(value)
    if parsed is None:
        return None
    return float(parsed)


def _normalize_drawdown_metric(value: object) -> float | None:
    parsed = _coerce_optional_float(value)
    if parsed is None:
        return None
    drawdown = abs(float(parsed))
    # Drawdown as a ratio cannot exceed 1.0; cap legacy additive artifacts.
    return float(min(drawdown, 1.0))


def _normalize_win_rate_metric(value: object) -> float | None:
    parsed = _coerce_optional_float(value)
    if parsed is None:
        return None
    win_rate = float(parsed)
    if abs(win_rate) > 1.0:
        win_rate = win_rate / 100.0
    return float(max(0.0, min(win_rate, 1.0)))


def _normalize_best_backtest_metrics(raw_metrics: object) -> dict:
    metrics = _parse_json_blob(raw_metrics, {})
    if not isinstance(metrics, dict):
        metrics = {}

    normalized: dict[str, object] = {}
    sharpe = _coerce_optional_float(metrics.get("sharpe_ratio"))
    if sharpe is None:
        sharpe = _coerce_optional_float(metrics.get("sharpe"))
    if sharpe is not None:
        normalized["sharpe"] = float(sharpe)
        normalized["sharpe_ratio"] = float(sharpe)

    total_return = _normalize_ratio_metric(
        metrics.get("total_return_pct")
        if metrics.get("total_return_pct") is not None
        else metrics.get("total_return")
    )
    if total_return is None:
        total_return = _normalize_ratio_metric(metrics.get("pnl_pct"))
    if total_return is None:
        total_return = _normalize_ratio_metric(metrics.get("return_pct"))
    if total_return is not None:
        normalized["total_return_pct"] = float(total_return)
        normalized["total_return"] = float(total_return)

    max_drawdown = _normalize_drawdown_metric(
        metrics.get("max_drawdown_pct")
        if metrics.get("max_drawdown_pct") is not None
        else metrics.get("max_drawdown")
    )
    if max_drawdown is None:
        max_drawdown = _normalize_drawdown_metric(metrics.get("drawdown_pct"))
    if max_drawdown is not None:
        normalized["max_drawdown_pct"] = float(max_drawdown)
        normalized["max_drawdown"] = float(max_drawdown)

    win_rate = _normalize_win_rate_metric(
        metrics.get("win_rate")
        if metrics.get("win_rate") is not None
        else metrics.get("winRate")
    )
    if win_rate is not None:
        normalized["win_rate"] = float(win_rate)
        normalized["winRate"] = float(win_rate)

    total_trades = _coerce_optional_float(
        metrics.get("total_trades")
        if metrics.get("total_trades") is not None
        else metrics.get("trades")
    )
    if total_trades is not None:
        normalized["total_trades"] = int(max(total_trades, 0.0))
        normalized["trades"] = int(max(total_trades, 0.0))

    profit_factor = _coerce_optional_float(
        metrics.get("profit_factor")
        if metrics.get("profit_factor") is not None
        else metrics.get("profitFactor")
    )
    if profit_factor is None:
        profit_factor = _coerce_optional_float(metrics.get("pf"))
    if profit_factor is not None:
        normalized["profit_factor"] = float(profit_factor)
        normalized["profitFactor"] = float(profit_factor)
        normalized["pf"] = float(profit_factor)

    # Keep raw result-level metadata available to UI consumers.
    for passthrough in ("robustness_score", "in_sample_sharpe", "out_of_sample_sharpe", "backtest_months", "annualized_return_pct", "monthly_return_pct"):
        if passthrough in metrics and metrics.get(passthrough) is not None:
            normalized[passthrough] = metrics.get(passthrough)

    return normalized


def _normalize_history_metrics(raw_metrics: object) -> dict:
    metrics = _parse_json_blob(raw_metrics, {})
    if not isinstance(metrics, dict):
        metrics = {}
    normalized = dict(metrics)
    normalized.update(_normalize_best_backtest_metrics(metrics))
    return normalized


def _best_backtest_rank_key(metrics: dict, created_at: str) -> tuple[int, float, float, float, float, int, float]:
    # Single source of truth (incl. the degenerate-slice trade floor) lives in
    # strategy_lifecycle; this module's enrichment is a legacy duplicate.
    from forven.strategy_lifecycle import _best_backtest_rank_key as _lifecycle_rank_key

    return _lifecycle_rank_key(metrics, created_at)


def _enrich_strategy_rows_with_best_backtest(rows: list[dict]) -> list[dict]:
    if not rows:
        return rows

    strategy_ids = [str(row.get("id") or "").strip() for row in rows]
    strategy_ids = [sid for sid in strategy_ids if sid]
    if not strategy_ids:
        return rows

    from forven.strategy_lifecycle import _symbol_base_asset

    market_by_strategy: dict[str, tuple[str, str]] = {
        sid: (
            _symbol_base_asset(row.get("symbol")),
            str(row.get("timeframe") or "").strip().lower(),
        )
        for row in rows
        if (sid := str(row.get("id") or "").strip())
    }

    best_by_strategy: dict[str, dict] = {}
    with get_db() as conn:
        chunk_size = 500
        for index in range(0, len(strategy_ids), chunk_size):
            chunk = strategy_ids[index:index + chunk_size]
            placeholders = ",".join(["?"] * len(chunk))
            sql = (
                "SELECT strategy_id, result_id, symbol, timeframe, config_json, metrics_json, created_at "
                "FROM backtest_results "
                f"WHERE strategy_id IN ({placeholders}) "
                "AND LOWER(TRIM(COALESCE(result_type, ''))) = 'backtest' "
                "AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')"
            )
            result_rows = conn.execute(sql, tuple(chunk)).fetchall()
            for result_row in result_rows:
                sid = str(result_row["strategy_id"] or "").strip()
                if not sid:
                    continue
                metrics = _normalize_best_backtest_metrics(result_row["metrics_json"])
                if not metrics:
                    continue
                # Scope "best" to the strategy's own market (see strategy_lifecycle —
                # a cross-asset/timeframe screen run must not define the card).
                from forven.strategy_lifecycle import (
                    _extract_symbol_timeframe_from_config,
                    _result_matches_strategy_market,
                )

                result_symbol = str(result_row["symbol"] or "").strip()
                result_timeframe = str(result_row["timeframe"] or "").strip()
                if not result_symbol or not result_timeframe:
                    cfg_symbol, cfg_timeframe = _extract_symbol_timeframe_from_config(result_row["config_json"])
                    result_symbol = result_symbol or (cfg_symbol or "")
                    result_timeframe = result_timeframe or (cfg_timeframe or "")
                strategy_market = market_by_strategy.get(sid, ("", ""))
                if not _result_matches_strategy_market(
                    strategy_market[0], strategy_market[1], result_symbol, result_timeframe
                ):
                    continue
                created_at = str(result_row["created_at"] or "")
                rank_key = _best_backtest_rank_key(metrics, created_at)
                existing = best_by_strategy.get(sid)
                if existing is None or rank_key > existing["rank_key"]:
                    best_by_strategy[sid] = {
                        "result_id": str(result_row["result_id"] or "").strip() or None,
                        "created_at": created_at or None,
                        "metrics": metrics,
                        "rank_key": rank_key,
                    }

    enriched_rows: list[dict] = []
    for row in rows:
        strategy_id = str(row.get("id") or "").strip()
        best = best_by_strategy.get(strategy_id)
        if not best:
            enriched_rows.append(row)
            continue

        merged = dict(row)
        current_metrics = _normalize_lifecycle_metrics(merged.get("metrics"))
        merged_metrics = dict(current_metrics)
        merged_metrics.update(best["metrics"])
        merged["strategy_metrics"] = current_metrics
        merged["metrics"] = merged_metrics
        merged["latest_metrics"] = best["metrics"]
        merged["backtest_metrics"] = best["metrics"]
        merged["best_backtest_result_id"] = best.get("result_id")
        merged["best_backtest_created_at"] = best.get("created_at")
        enriched_rows.append(merged)
    return enriched_rows


def read_strategies(status: str | None = None, limit: int | None = None, offset: int = 0):
    return lifecycle_service.read_strategies(status=status, limit=limit, offset=offset)


def promote_strategy(strategy_id: str, body: StrategyPromoteBody):
    return lifecycle_service.promote_strategy(strategy_id, body)


def read_lifecycle_strategies(
    state: str | None = None,
    source: str | None = None,
    symbol: str | None = None,
    name: str | None = None,
    source_ref: str | None = None,
    limit: int = 500,
    offset: int = 0,
):
    return lifecycle_service.read_lifecycle_strategies(
        state=state,
        source=source,
        symbol=symbol,
        name=name,
        source_ref=source_ref,
        limit=limit,
        offset=offset,
    )


def read_lifecycle_strategy(strategy_id: str):
    return lifecycle_service.read_lifecycle_strategy(strategy_id)


def get_strategy_container(
    strategy_id: str,
    result_limit: int = 200,
    trade_limit: int = 500,
):
    return lifecycle_service.get_strategy_container(
        strategy_id,
        result_limit=result_limit,
        trade_limit=trade_limit,
    )


def create_lifecycle_strategy(body: LifecycleCreateBody):
    return lifecycle_service.create_lifecycle_strategy(body)


def transition_lifecycle_strategy(body: LifecycleTransitionBody):
    return lifecycle_service.transition_lifecycle_strategy(body)


def read_lifecycle_events(limit: int = 100):
    return lifecycle_service.read_lifecycle_events(limit=limit)

def read_agents(enabled_only: bool = False):
    rows = get_agents(enabled_only=enabled_only)
    return [_inject_agent_role_from_workspace(agent) for agent in rows]


def get_agent_model_options(refresh: bool = False):
    return _legacy_agent_model_options(force_refresh=refresh)


def upsert_auth_provider(provider: str, body: AuthProviderProfileBody):
    normalized_provider = _normalize_auth_provider(provider)
    existing_profile = get_profile(normalized_provider) or {}

    access_token = str((body.access_token or body.access or body.token or body.api_key or "").strip())
    refresh_token = body.refresh_token or body.refresh
    expires_ms = _coerce_profile_expiry(body)
    base_url = str(body.base_url or "").strip()

    profile = dict(existing_profile)
    if access_token:
        profile["access"] = access_token
    elif normalized_provider != "lmstudio" and not existing_profile:
        raise HTTPException(status_code=400, detail=f"access token required to create profile for {normalized_provider}")

    if refresh_token is not None:
        if str(refresh_token).strip():
            profile["refresh"] = str(refresh_token).strip()
        else:
            profile.pop("refresh", None)

    if expires_ms is not None:
        profile["expires"] = expires_ms

    if normalized_provider == "lmstudio":
        if base_url:
            profile["base_url"] = _normalize_local_base_url(normalized_provider, base_url)
        elif not profile.get("base_url"):
            raise HTTPException(status_code=400, detail="base_url required to create profile for lmstudio")
        profile.pop("refresh", None)
        profile.pop("expires", None)
    elif normalized_provider == "zai" and base_url:
        profile["base_url"] = _normalize_local_base_url(normalized_provider, base_url, use_default=False)

    if not profile:
        raise HTTPException(status_code=400, detail=f"invalid credentials payload for {normalized_provider}")

    # Reject a definitively-invalid key at entry time. Only when a new token is
    # being set (not a base_url-only update) and never for lmstudio (local, no
    # key to verify). _verify_provider_key raises HTTPException(400) on a hard
    # rejection (400/401/403); transient/unverifiable outcomes are tolerated so
    # a network blip can't block a legitimate save.
    if access_token and normalized_provider != "lmstudio":
        _verify_provider_key(normalized_provider, access_token)

    upsert_profile(normalized_provider, profile)
    # Record an explicit in-app connection so the fail-closed model gate treats
    # this provider as usable (an env-var key alone never authorizes spend).
    try:
        from forven.model_selection import mark_provider_connected

        mark_provider_connected(normalized_provider)
    except Exception:
        log.exception("failed to mark %s connected", normalized_provider)
    return {"ok": True, "provider": normalized_provider}


def delete_auth_provider(provider: str):
    normalized_provider = _normalize_auth_provider(provider)
    removed = delete_profile(normalized_provider)
    # Forget the in-app connection so the provider can no longer authorize spend.
    try:
        from forven.model_selection import unmark_provider_connected

        unmark_provider_connected(normalized_provider)
    except Exception:
        log.exception("failed to unmark %s", normalized_provider)
    if not removed:
        return {"ok": False, "provider": normalized_provider, "removed": False}
    return {"ok": True, "provider": normalized_provider, "removed": True}


def _verify_provider_key(provider: str, token: str) -> tuple[str, str]:
    """Probe *provider* to check *token* is actually valid.

    Returns ``(state, message)`` where state is:
      - ``"ok"``           — provider accepted the key (HTTP 200/429)
      - ``"no_endpoint"``  — no way to verify this provider remotely
      - ``"unreachable"``  — had an endpoint but it didn't answer (404/5xx/network)

    Raises ``HTTPException(400)`` when the provider *definitively* rejects the
    key (HTTP 400/401/403) — that is a real "bad key" signal, distinct from a
    transient failure callers may choose to tolerate.
    """
    # A ChatGPT OAuth token is valid against the Codex backend but always 401s
    # against api.openai.com/v1/models — probing there would misreport a working
    # OAuth connection as a bad key. The token was minted via the TLS OAuth
    # exchange and get_token() already refreshed it if expired, so treat a
    # well-formed OAuth token as connected.
    if provider == "openai" and is_openai_oauth_token(token):
        return "ok", "Connected (ChatGPT OAuth)"

    endpoints = (
        _AUTH_TEST_ENDPOINT_OVERRIDES.get(provider)
        or _MODEL_DISCOVERY_ALT_ENDPOINTS.get(provider, [])
    )
    headers_template = (
        _AUTH_TEST_HEADER_OVERRIDES.get(provider)
        or _MODEL_DISCOVERY_HEADERS.get(provider, {})
    )
    if not (endpoints and headers_template):
        return "no_endpoint", "not verifiable for this provider"

    header = {key: value.format(token=token) for key, value in headers_template.items()}
    last_error: str | None = None
    for endpoint in endpoints:
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(endpoint, headers=header)
        except Exception as exc:
            last_error = str(exc)
            continue
        code = response.status_code
        if code == 200:
            try:
                models = _extract_discovery_models(response.json(), provider)
                note = f" ({len(models)} models available)" if models else ""
            except Exception:
                note = ""
            return "ok", f"Connected{note}"
        if code == 429:
            return "ok", "Key valid (rate-limited at test time)"
        if code in (400, 401, 403):
            raise HTTPException(
                status_code=400,
                detail=f"{provider}: invalid API key (HTTP {code})",
            )
        last_error = f"HTTP {code}"
        continue

    return "unreachable", last_error or "no endpoint responded"


def test_auth_provider(provider: str):
    normalized_provider = _normalize_auth_provider(provider)
    if not get_profile(normalized_provider):
        raise HTTPException(status_code=404, detail=f"provider profile not configured: {normalized_provider}")

    if normalized_provider == "lmstudio":
        profile = get_profile(normalized_provider) or {}
        base_url = _get_provider_base_url(normalized_provider, profile)
        if not base_url:
            raise HTTPException(status_code=400, detail="lmstudio base_url missing")
        token = str(profile.get("access") or profile.get("token") or profile.get("api_key") or "").strip()
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            with httpx.Client(timeout=15) as client:
                response = client.get(f"{base_url}/v1/models", headers=headers)
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        models = _extract_discovery_models(payload, normalized_provider)
        return {
            "ok": True,
            "provider": normalized_provider,
            "status": _build_auth_provider_payload(normalized_provider)["status"],
            "message": f"Connected to LM Studio ({len(models)} models discovered)",
        }

    try:
        token = get_token(normalized_provider)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not token:
        raise HTTPException(status_code=400, detail=f"{normalized_provider} token missing after load")

    # Verify the key against the provider — a present-but-invalid key must fail.
    # _verify_provider_key raises on a definitive rejection (400/401/403).
    state, message = _verify_provider_key(normalized_provider, token)
    if state == "unreachable":
        # Test is strict: an endpoint exists but we couldn't confirm the key.
        raise HTTPException(
            status_code=400,
            detail=f"{normalized_provider}: could not verify key ({message})",
        )
    return {
        "ok": True,
        "provider": normalized_provider,
        "status": _build_auth_provider_payload(normalized_provider)["status"],
        "message": message if state == "ok" else "Token saved (not verified against provider)",
    }


def get_auth_providers():
    return _get_auth_providers_compat()


def get_model_policy():
    return _get_model_policy_compat()


def put_model_policy(body: ModelPolicyUpdateBody):
    return _update_model_policy(body)


def put_legacy_model_policy(body: ModelPolicyUpdateBody):
    return _update_model_policy(body)


def get_agent(agent_id: str):
    agent = _lookup_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
    return _inject_agent_role_from_workspace(agent) or agent


# Canonical built-in agent IDs that cannot be deleted via the API. These are
# the system-seeded workers wired into brain.py / scheduler / routing and would
# break startup if removed. Custom strategy-developer agents live outside this
# set and can be freely created/removed from the Agent Hub UI.
_PROTECTED_AGENT_IDS: frozenset[str] = frozenset(
    {
        "brain",
        "quant-researcher",
        "simulation-agent",
        "risk-manager",
        # execution-trader retired 2026-06-30 (deleted via deprecated_agents) —
        # no longer a protected system agent.
        "full-stack-engineer",
        "strategy-developer",
    }
)


def _slugify_agent_id(name: str) -> str:
    text = str(name or "").strip().lower()
    cleaned = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return cleaned


def post_strategy_developer_agent(payload: LegacyAgentCreateBody) -> dict:
    """Create a new strategy-developer agent. Role is forced — the Hub UI only
    adds developers; arbitrary role creation is not exposed."""
    name = str(payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    slug_base = _slugify_agent_id(name)
    if not slug_base:
        raise HTTPException(status_code=400, detail="name must contain letters or digits")

    # Reserve a unique agent_id: prefer the exact slug, fall back to slug-2, slug-3, ...
    with get_db() as conn:
        existing_ids = {
            str(row["id"]).strip().lower()
            for row in conn.execute("SELECT id FROM agents").fetchall()
        }

    agent_id = slug_base
    if agent_id in existing_ids or agent_id in _PROTECTED_AGENT_IDS:
        suffix = 2
        while True:
            candidate = f"{slug_base}-{suffix}"
            if candidate not in existing_ids and candidate not in _PROTECTED_AGENT_IDS:
                agent_id = candidate
                break
            suffix += 1

    model = str(payload.model or "openai").strip() or "openai"
    model_id = payload.model_id
    normalized_model, normalized_model_id = normalize_provider_and_model(model, model_id)

    create_agent(
        agent_id=agent_id,
        name=name,
        role="strategy-developer",
        model=normalized_model,
        model_id=normalized_model_id,
        visibility="visible",
        instructions=payload.instructions,
    )
    log_activity(
        "info",
        "agents",
        f"Created strategy-developer agent {agent_id} ({name})",
    )
    return get_agent(agent_id)


def delete_agent_row(agent_id: str) -> dict:
    normalized = str(agent_id or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="agent_id is required")
    if normalized in _PROTECTED_AGENT_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"agent {normalized!r} is a core system agent and cannot be deleted",
        )
    existing = _lookup_agent(normalized)
    if not existing:
        raise HTTPException(status_code=404, detail=f"agent not found: {normalized}")
    delete_agent(normalized)
    log_activity("info", "agents", f"Deleted agent {normalized}")
    return {"ok": True, "deleted_agent_id": normalized}


def patch_agent(agent_id: str, payload: LegacyAgentUpdateBody):
    if not _lookup_agent(agent_id):
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")

    updates = payload.dict(exclude_none=True)
    if "model" in updates or "model_id" in updates:
        current = _lookup_agent(agent_id) or {}
        model, model_id = normalize_provider_and_model(
            updates.get("model", current.get("model")),
            updates.get("model_id", current.get("model_id")),
        )
        updates["model"] = model
        updates["model_id"] = model_id
    if "visibility" in updates:
        updates["visibility"] = normalize_agent_visibility(updates.get("visibility"))
    if updates:
        update_agent(agent_id, **updates)

    return get_agent(agent_id)


def get_agent_documents(agent_id: str):
    docs = _build_agent_documents(agent_id)
    if not _lookup_agent(agent_id) and not (docs.get("soul") or docs.get("agents") or docs.get("role")):
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
    return docs


def get_agent_document(agent_id: str, document: str):
    payload = _build_agent_documents(agent_id)
    if not _lookup_agent(agent_id) and not (payload.get("soul") or payload.get("agents") or payload.get("role")):
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
    if document not in payload:
        raise HTTPException(status_code=404, detail=f"document not found: {document}")
    return {"document": document, "content": payload[document]}


def put_agent_document(agent_id: str, document: str, payload: LegacyAgentDocumentBody):
    if not _lookup_agent(agent_id):
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")

    key = document.strip().lower()
    content = payload.content or ""
    # SOUL.md and AGENTS.md are now PER-AGENT — write the edited content to the
    # agent's own copy (agents/<id>/...) rather than the shared global file, so
    # editing one agent's identity never bleeds into the others.
    if key == "soul":
        write_workspace(f"agents/{agent_id}/SOUL.md", content)
    elif key == "agents":
        write_workspace(f"agents/{agent_id}/AGENTS.md", content)
    elif key == "role":
        write_workspace(f"agents/{agent_id}/ROLE.md", content)
        update_agent(agent_id, role=content)
    else:
        raise HTTPException(status_code=400, detail=f"unsupported document: {document}")

    return {"ok": True}


def patch_agent_model(agent_id: str, payload: LegacyAgentModelBody):
    if not _lookup_agent(agent_id):
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
    model = payload.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    model, model_id = normalize_provider_and_model(model, payload.model_id)
    update_agent(
        agent_id,
        model=model,
        model_id=model_id,
    )
    response = get_agent(agent_id)
    # Additive, backward-compatible: warn (without blocking the save) when the
    # selected provider is not connected, so the operator knows this agent's
    # model will not run until they connect it. Runtime fails closed anyway.
    warnings: list[dict] = []
    if not _provider_is_connected_safe(model):
        warnings.append(_not_connected_warning(str(model or "").strip().lower(), model_id))
    if isinstance(response, dict):
        response["warnings"] = warnings
    return response


def post_agent_test_discord(agent_id: str, payload: AgentDiscordTestBody | None = None):
    normalized_agent_id = str(agent_id or "").strip()
    if not normalized_agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required")

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name FROM agents WHERE id = ?",
            (normalized_agent_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail=f"agent not found: {normalized_agent_id}")

    agent_name = str(row["name"] or normalized_agent_id).strip() or normalized_agent_id
    override_token = str((payload.discord_token if payload else "") or "").strip()
    token = override_token
    if not token:
        try:
            from forven.bot import get_bot_token

            token = str(get_bot_token() or "").strip()
        except Exception:
            token = ""
    if not token:
        raise HTTPException(
            status_code=400,
            detail="Gateway Discord bot token is not configured. Configure the main bot token first, or provide one in this test request.",
        )

    from forven.bot import CHANNELS
    from forven.reporter import AGENT_CHANNEL_MAP

    channel_name = AGENT_CHANNEL_MAP.get(normalized_agent_id, "research")
    channel_id = CHANNELS.get(channel_name)
    if not channel_id:
        raise HTTPException(status_code=500, detail=f"Discord channel mapping missing for '{channel_name}'")

    tested_at = _now()
    message = (
        f"[Forven Settings] Gateway Discord test message for {agent_name} ({normalized_agent_id}) at {tested_at}.\n"
        "If you can read this, the gateway bot can post to this channel."
    )
    headers = {
        "Authorization": f"Bot {token}",
        "Content-Type": "application/json",
    }

    try:
        response = httpx.post(
            f"https://discord.com/api/v10/channels/{channel_id}/messages",
            headers=headers,
            json={"content": message[:1800]},
            timeout=10,
        )
    except Exception as exc:
        log.exception("Discord request failed")
        raise HTTPException(status_code=502, detail=f"Discord request failed: {exc}") from exc

    if response.status_code not in (200, 201):
        raw = (response.text or "").strip()
        detail = f"Discord rejected test message ({response.status_code})"
        if raw:
            detail = f"{detail}: {raw[:400]}"
        raise HTTPException(status_code=400, detail=detail)

    log_activity(
        "info",
        "settings",
        f"Agent Discord test message sent for {normalized_agent_id} to #{channel_name}",
        {
            "agent_id": normalized_agent_id,
            "channel_name": channel_name,
            "channel_id": channel_id,
        },
    )

    return {
        "status": "ok",
        "agent_id": normalized_agent_id,
        "agent_name": agent_name,
        "channel": channel_name,
        "channel_id": str(channel_id),
        "tested_at": tested_at,
    }


def get_agent_terminal(agent_id: str):
    if not _lookup_agent(agent_id):
        raise HTTPException(status_code=404, detail=f"agent not found: {agent_id}")
    docs = _build_agent_documents(agent_id)
    with get_db() as conn:
        source_prefix = f"agent:{agent_id}"
        source_like = f"{source_prefix}:%"
        logs = conn.execute(
            "SELECT * FROM activity_log "
            "WHERE source = ? OR source LIKE ? "
            "ORDER BY created_at DESC LIMIT 50",
            (source_prefix, source_like),
        ).fetchall()
        logs_payload = [dict(log_row) for log_row in logs]
        # Recent task "calls": the request + model response + per-provider attempt
        # trace (with error bodies) so the Logs tab can show the full request->response
        # back-and-forth and exactly WHY a provider failed (incl. masked fallback hops).
        try:
            call_rows = conn.execute(
                "SELECT id, title, status, provider, model_id, output_data, error, "
                "created_at, completed_at "
                "FROM agent_tasks WHERE agent_id = ? "
                "ORDER BY id DESC LIMIT 25",
                (agent_id,),
            ).fetchall()
            calls_payload = [dict(r) for r in call_rows]
            # The Brain reasons via a separate brain_invoke task (different table);
            # fold those in so its terminal shows its real request->response decisions
            # (and per-provider trace), not just the RAG recall lookups.
            if agent_id == "brain":
                brain_rows = conn.execute(
                    "SELECT id, status, result, error, created_at, completed_at "
                    "FROM tasks WHERE type='brain_invoke' ORDER BY id DESC LIMIT 25"
                ).fetchall()
                for r in brain_rows:
                    d = dict(r)
                    calls_payload.append({
                        "id": d["id"],
                        "title": f"Brain cycle #{d['id']}",
                        "status": d["status"],
                        "provider": None,
                        "model_id": None,
                        "output_data": d.get("result"),
                        "error": d.get("error"),
                        "created_at": d["created_at"],
                        "completed_at": d.get("completed_at"),
                    })
                # Normalize the two created_at formats (ISO 'T' vs space) before sorting.
                calls_payload.sort(
                    key=lambda c: str(c.get("created_at") or "").replace("T", " ")[:19],
                    reverse=True,
                )
                calls_payload = calls_payload[:40]
        except Exception:
            calls_payload = []
    details = inspect_agent(agent_id)
    return {
        "memory": docs.get("soul"),
        "documents": docs,
        "agent": details,
        "logs": logs_payload,
        "calls": calls_payload,
    }


_PAPER_TEST_SETTING_KEYS = (
    "throughput_auto_scheduler_control",
    "scanner_execution_enabled",
    "relaxed_trade_filters_enabled",
    "strict_regime_gating",
    "allow_unknown_regime_strategies",
    "scanner_signal_interval_minutes",
    "scanner_execution_interval_minutes",
    "paper_test_mode_enabled",
    "paper_test_high_activity_enabled",
    "paper_test_bypass_gates_enabled",
    "paper_test_local_execution_only",
)


# â”€â”€ Phase 1A: New GET endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _auto_trash_failed_local_backtests(records: list[dict], deleted_ids: set[str]) -> set[str]:
    """Enforce policy on existing local results so failed noise is hidden automatically."""
    if not records:
        return set()

    def _record_requests_preservation(record: dict) -> bool:
        meta = record.get("metadata", {}) if isinstance(record, dict) else {}
        if not isinstance(meta, dict):
            meta = {}
        if _coerce_bool(meta.get("preserve_result"), False):
            return True
        config = _parse_json_blob(meta.get("config_json"), {})
        return isinstance(config, dict) and _coerce_bool(config.get("preserve_result"), False)

    def _stored_result_requests_preservation(conn, result_id: str) -> bool:
        row = conn.execute(
            "SELECT config_json FROM backtest_results WHERE result_id = ?",
            (result_id,),
        ).fetchone()
        if not row:
            return False
        config = _parse_json_blob(row["config_json"], {})
        return isinstance(config, dict) and _coerce_bool(config.get("preserve_result"), False)

    marked: set[str] = set()
    try:
        with get_db() as conn:
            for rec in records:
                rid = str(rec.get("id") or "").strip()
                if not rid or rid in marked:
                    continue
                preserved = _record_requests_preservation(rec) or _stored_result_requests_preservation(conn, rid)
                if preserved:
                    if rid in deleted_ids:
                        _set_backtest_result_trash(conn, rid, deleted=False)
                        deleted_ids.discard(rid)
                    continue
                if rid in deleted_ids:
                    continue
                summary = _normalize_backtest_summary(rec)
                should_trash, reason = _should_auto_trash_backtest_result(
                    total_return_pct=float(summary.get("total_return") or 0.0),
                    sharpe=float(summary.get("sharpe_ratio") or 0.0),
                    max_drawdown_ratio=float(summary.get("max_drawdown") or 0.0),
                    total_trades=int(summary.get("total_trades") or 0),
                )
                if not should_trash:
                    continue
                _set_backtest_result_trash(conn, rid, deleted=True)
                marked.add(rid)
                if len(marked) <= 5:
                    log.info("Auto-trashed existing backtest result %s (%s)", rid, reason or "policy")
        if len(marked) > 5:
            log.info("Auto-trashed %d additional existing backtest results (policy sweep).", len(marked) - 5)
    except Exception as exc:
        log.warning("Failed to auto-trash existing backtest results: %s", exc)
    return marked


def get_backtest_results(
    strategy: str | None = None,
    symbol: str | None = None,
    limit: int = 200,
    remote_skip: bool = False,
    lifecycle_id: str | None = None,
):
    """List backtest results for the Backtest Manager grid."""
    normalized_strategy = strategy.strip().lower() if strategy else None
    normalized_symbol = symbol.strip().upper() if symbol else None
    normalized_lifecycle = lifecycle_id.strip().upper() if lifecycle_id else None
    normalized_limit = max(1, int(limit))

    with get_db() as conn:
        deleted = _get_backtest_result_deleted_ids(conn)

    local_rows = _sqlite_backtest_summaries(
        strategy=strategy,
        symbol=symbol,
        lifecycle_id=lifecycle_id,
        limit=normalized_limit,
        deleted_ids=deleted,
    )

    # Chroma result listing has caused hard process resets on Windows. Use it
    # only as a legacy fallback when SQLite has nothing for this query.
    if not local_rows:
        records = _chroma_backtest_records()
        if records:
            newly_deleted = _auto_trash_failed_local_backtests(records, deleted)
            if newly_deleted:
                deleted.update(newly_deleted)

        for rec in records:
            meta = rec.get("metadata") or {}
            sid = str(meta.get("strategy_id", ""))
            if normalized_strategy and normalized_strategy not in sid.lower():
                continue
            if normalized_symbol and str(meta.get("asset", "")).upper() != normalized_symbol:
                continue
            if normalized_lifecycle:
                lsid = str(meta.get("lifecycle_strategy_id", "")).strip().upper()
                if lsid != normalized_lifecycle:
                    continue
            if rec.get("id") in deleted:
                continue
            local_rows.append(_normalize_backtest_summary(rec))

    remote_rows: list[dict] = []
    # Skip remote when filtering by lifecycle_id â€” container history is always local.
    if not remote_skip and not normalized_lifecycle:
        remote_rows = _fetch_remote_backtest_summaries(
            strategy=strategy,
            symbol=symbol,
            limit=normalized_limit,
            log_errors=True,
        )
        if _is_remote_configured() and not remote_rows and not _is_remote_backtest_results_available():
            remote_api = _resolve_backtest_results_remote_api()
            raise HTTPException(
                status_code=503,
                detail=f"Remote backtest source is configured but unreachable: {remote_api}",
            )

    merged_by_id: dict[str, dict] = {}
    for row in [*local_rows, *remote_rows]:
        rid = str(row.get("id") or "").strip()
        if not rid or rid in merged_by_id or rid in deleted:
            continue
        merged_by_id[rid] = row

    merged = list(merged_by_id.values())
    merged.sort(key=lambda row: _to_datetime_sort_key(row.get("created_at")), reverse=True)
    return json_safe_payload(merged[:normalized_limit])


def update_backtest_result_params(result_id: str, new_params: dict) -> dict:
    """Update the parameters in an existing backtest result's config_json."""
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT config_json FROM backtest_results WHERE result_id = ?",
                (result_id,),
            ).fetchone()
            if row:
                config = json.loads(row["config_json"] or "{}")
                config["params"] = new_params
                conn.execute(
                    "UPDATE backtest_results SET config_json = ? WHERE result_id = ?",
                    (json.dumps(config, separators=(",", ":"), default=str), result_id),
                )
    except Exception:
        pass

    return {"ok": True, "result_id": result_id, "updated_params": new_params}


def update_strategy_default_params(
    strategy_id: str,
    new_params: dict,
    pinned_backtest_id: str | None = None,
    *,
    actor: str = "ui",
) -> dict:
    """Update a strategy's default parameters (used for paper/live trading).

    This is the USER path: an explicit operator override (Set-Default UI / API /
    deepdive chat). The ``actor`` is forwarded to ``brain.update_strategy_params``
    so the param-lock that freezes paper/live strategies against automated writers
    is bypassed for genuine user actions, and the override is recorded as a
    strategy_event for audit.

    When ``pinned_backtest_id`` is provided (truthy), the strategy is marked
    as pinned to that backtest result — lab-manager enrichment then displays
    that row's metrics instead of auto-selecting the top-ranked backtest.
    Passing an explicit empty string ("") clears any existing pin. Passing
    ``None`` leaves the pin untouched.
    """
    with get_db() as conn:
        row = conn.execute("SELECT id, type, params, stage FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Strategy {strategy_id} not found")

    from forven.strategies.certification import certify_execution_strategy
    from forven.brain import update_strategy_params

    existing_params = _parse_strategy_params_blob(row["params"])
    incoming_params = new_params if isinstance(new_params, dict) else {}
    merged_params = {**existing_params, **incoming_params}

    certification = certify_execution_strategy(row["type"], merged_params)
    certification_error = certification.format_error(context="params")
    if certification_error:
        raise HTTPException(status_code=422, detail=certification_error)

    # Refuse a cross-asset pin onto a capital-adjacent strategy fail-fast, BEFORE any
    # write: its traded asset is frozen, so pinning a different-asset backtest is
    # meaningless and would mislead the operator (the symbol sync would no-op anyway).
    if pinned_backtest_id:
        _pin_id = pinned_backtest_id.strip() if isinstance(pinned_backtest_id, str) else ""
        if _pin_id:
            from forven.db import capital_adjacent_pin_asset_conflict
            from forven.strategy_lifecycle import _extract_symbol_timeframe_from_config

            with get_db() as conn:
                _pin_row = conn.execute(
                    "SELECT symbol, config_json FROM backtest_results "
                    "WHERE result_id = ? AND strategy_id = ?",
                    (_pin_id, strategy_id),
                ).fetchone()
                if _pin_row is not None:
                    _cfg_symbol, _ = _extract_symbol_timeframe_from_config(_pin_row["config_json"])
                    _pin_symbol = _cfg_symbol or (
                        str(_pin_row["symbol"]).strip() if _pin_row["symbol"] else None
                    )
                    conflict, _stage, _cur = capital_adjacent_pin_asset_conflict(
                        conn, strategy_id, _pin_symbol
                    )
                    if conflict:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Cannot pin a {_pin_symbol} backtest to {strategy_id}: it is "
                                f"{_stage} and its traded asset ({_cur}) is frozen. Pin a "
                                "same-asset backtest, or re-home the strategy before promotion."
                            ),
                        )

    canonical_params = dict(certification.canonical_params)
    update_strategy_params(strategy_id, canonical_params, actor=actor)

    # Audit the user override (Set-Default / API / deepdive). from==to stage: this
    # is a params change, not a stage transition.
    try:
        from forven.db import append_strategy_event

        current_stage = str(row["stage"] or "").strip() or None
        append_strategy_event(
            strategy_id,
            from_state=current_stage,
            to_state=current_stage or "",
            actor=actor,
            reason="user default-params override",
            details={"params_keys": list(incoming_params.keys())},
        )
    except Exception:  # noqa: BLE001 - audit is best-effort, never blocks the write
        pass

    pin_written: str | None = None
    if pinned_backtest_id is not None:
        pin_value = pinned_backtest_id.strip() if isinstance(pinned_backtest_id, str) else ""
        pin_to_store: str | None = pin_value if pin_value else None
        with get_db() as conn:
            conn.execute(
                "UPDATE strategies SET pinned_backtest_id = ? WHERE id = ?",
                (pin_to_store, strategy_id),
            )
            if pin_to_store:
                # Protect the pinned row from retention: clear any prior soft-delete marker
                # so enrichment can find it and include its metrics in the lab manager.
                conn.execute(
                    "UPDATE backtest_results SET deleted_at = NULL WHERE result_id = ? AND strategy_id = ?",
                    (pin_to_store, strategy_id),
                )
                # Sync runtime fields from the pinned backtest. The paper scanner and live
                # engine read strategies.timeframe / strategies.symbol directly, so without
                # this a 5m-pinned strategy would keep running on its creation-time 1h.
                pin_row = conn.execute(
                    "SELECT symbol, timeframe, config_json FROM backtest_results "
                    "WHERE result_id = ? AND strategy_id = ?",
                    (pin_to_store, strategy_id),
                ).fetchone()
                if pin_row is not None:
                    from forven.strategy_lifecycle import _extract_symbol_timeframe_from_config
                    cfg_symbol, cfg_timeframe = _extract_symbol_timeframe_from_config(pin_row["config_json"])
                    pin_symbol = cfg_symbol or (str(pin_row["symbol"]).strip() if pin_row["symbol"] else None)
                    pin_timeframe = cfg_timeframe or (str(pin_row["timeframe"]).strip() if pin_row["timeframe"] else None)
                    sync_cols: list[str] = []
                    sync_vals: list[str] = []
                    if pin_timeframe:
                        sync_cols.append("timeframe = ?")
                        sync_vals.append(pin_timeframe)
                    if pin_symbol:
                        # Traded-asset freeze: a pinned backtest on a different asset
                        # must not flip a running paper/live strategy's traded coin.
                        # (Timeframe still syncs above — only the asset is frozen.)
                        from forven.db import block_cross_asset_symbol_rehome

                        if not block_cross_asset_symbol_rehome(
                            conn, strategy_id, pin_symbol, source="pinned_backtest_sync"
                        ):
                            sync_cols.append("symbol = ?")
                            sync_vals.append(pin_symbol)
                    if sync_cols:
                        sync_vals.append(strategy_id)
                        conn.execute(
                            f"UPDATE strategies SET {', '.join(sync_cols)} WHERE id = ?",
                            tuple(sync_vals),
                        )
        pin_written = pin_to_store

    # Research recovery: on param edit, try re-certification for research_only strategies
    _try_research_recovery_on_edit(strategy_id)

    # Propagate execution-setting changes onto an OPEN paper/live position so the
    # edit "takes" on the running trade. Only when the strategy is in an
    # operator-owned (paper/live) stage AND the execution_profile actually changed —
    # a pure alpha-param edit leaves the open position's SL/TP alone. Best-effort:
    # never fail the param save on a downstream exchange hiccup.
    open_position_update = None
    try:
        from forven.brain import stage_is_param_locked
        from forven.strategies import sizing as _sizing

        if stage_is_param_locked(row["stage"]):
            old_ep = _sizing.normalize_execution_controls(_sizing.extract_execution_profile(existing_params))
            new_ep = _sizing.normalize_execution_controls(_sizing.extract_execution_profile(canonical_params))
            if old_ep != new_ep:
                from forven.api_domains.paper_control import apply_execution_profile_to_open_position

                open_position_update = apply_execution_profile_to_open_position(
                    strategy_id, canonical_params, actor=actor
                )
    except Exception:  # noqa: BLE001 — propagation is best-effort; the save already succeeded
        log.warning("execution-profile propagation to open position failed for %s", strategy_id, exc_info=True)

    return {
        "ok": True,
        "strategy_id": strategy_id,
        "params": canonical_params,
        "pinned_backtest_id": pin_written,
        "open_position_update": open_position_update,
    }


def _try_research_recovery_on_edit(strategy_id: str):
    """Debounced research recovery trigger on param edit. Max 1 per strategy per 5 min."""
    try:
        from forven.db import get_db as _gdb, kv_get as _kvg, kv_set as _kvs
        from datetime import datetime as _dt, timezone as _tz

        # Check if strategy is research_only
        with _gdb() as conn:
            row = conn.execute(
                "SELECT stage FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
        if not row or (row["stage"] or "").strip().lower() != "research_only":
            return

        # Debounce: 1 per strategy per 5 min
        debounce_key = f"forven:recert_debounce:{strategy_id}"
        last_run = _kvg(debounce_key)
        if last_run:
            try:
                last_dt = _dt.fromisoformat(last_run)
                if (_dt.now(_tz.utc) - last_dt).total_seconds() < 300:
                    return
            except Exception:
                pass

        _kvs(debounce_key, _dt.now(_tz.utc).isoformat())

        from forven.brain import try_research_recovery
        result = try_research_recovery(strategy_id)

        # WebSocket broadcast if available
        if result.get("promoted"):
            # API-08: this runs in the threadpool (sync handler), so the old
            # `asyncio.run(ws_manager.broadcast(...))` fallback drove sockets owned
            # by the uvicorn loop from a throwaway loop — and the bare
            # `except Exception: pass` around it meant the resulting failure was
            # invisible: the certification_change event silently never arrived.
            dispatch_ws_broadcast(
                {"type": "certification_change", "strategy_id": strategy_id, "promoted": True}
            )
    except Exception:
        import logging
        logging.getLogger("forven.api_core").warning(
            "Research recovery on edit failed for %s", strategy_id, exc_info=True
        )


def get_backtest_results_count(
    since: str | None = None,
    strategy: str | None = None,
    symbol: str | None = None,
    remote_skip: bool = False,
):
    """Count non-deleted backtest results (optionally scoped by strategy/symbol)."""
    normalized_strategy = strategy.strip().lower() if strategy else None
    normalized_symbol = symbol.strip().upper() if symbol else None

    with get_db() as conn:
        deleted = _get_backtest_result_deleted_ids(conn)

    remote_enabled, remote_api = _resolve_remote_backtesting_mode()
    if remote_enabled and not remote_skip:
        if not remote_api:
            raise HTTPException(
                status_code=503,
                detail="Remote backtest mode is enabled, but no remote API URL is configured.",
            )
        remote_rows = _fetch_remote_backtest_results(
            strategy=normalized_strategy,
            symbol=normalized_symbol,
            limit=10_000,
        )
        matched_remote: set[str] = set()
        for row in remote_rows:
            rid = str(row.get("id") or "").strip()
            if not rid or rid in deleted:
                continue
            if since:
                created_at = str(row.get("created_at") or "")
                if created_at and created_at < since:
                    continue
            matched_remote.add(rid)
        if not matched_remote and not _is_remote_backtesting_reachable(remote_api):
            raise HTTPException(
                status_code=503,
                detail=f"Remote backtest source is enabled but unreachable: {remote_api}",
            )
        return {"count": len(matched_remote)}

    results = _chroma_backtest_records()
    if results:
        newly_deleted = _auto_trash_failed_local_backtests(results, deleted)
        if newly_deleted:
            deleted.update(newly_deleted)
    matched: set[str] = set()
    for rec in results:
        meta = rec.get("metadata") or {}
        rid = str(rec.get("id") or "").strip()
        if not rid or rid in deleted:
            continue
        sid = str(meta.get("strategy_id") or "").lower()
        sname = str(meta.get("strategy_name") or "").lower()
        if normalized_strategy and normalized_strategy not in sid and normalized_strategy not in sname:
            continue
        if normalized_symbol and str(meta.get("asset") or "").upper() != normalized_symbol:
            continue
        if since:
            try:
                if rec.get("metadata", {}).get("recorded_at") and rec["metadata"]["recorded_at"] < since:
                    continue
            except Exception:
                pass
        matched.add(rid)
    return {"count": len(matched)}


def get_backtest_result(result_id: str, remote_skip: bool = False):
    """Get detailed result for a specific backtest record."""
    if result_id == "trash":
        return get_backtest_trash()

    remote_enabled, remote_api = _resolve_remote_backtesting_mode()
    if remote_enabled and not remote_skip:
        if not remote_api:
            raise HTTPException(
                status_code=503,
                detail="Remote backtest mode is enabled, but no remote API URL is configured.",
            )
        remote = _fetch_remote_backtest_result(result_id)
        if remote:
            return json_safe_payload(remote)
        if not _is_remote_backtesting_reachable(remote_api):
            raise HTTPException(
                status_code=503,
                detail=f"Remote backtest source is enabled but unreachable: {remote_api}",
            )
        raise HTTPException(status_code=404, detail="result not found")

    # Try SQLite first (fast, reliable) before ChromaDB which can crash.
    sqlite_detail = _build_sqlite_backtest_detail(result_id)
    if sqlite_detail:
        return json_safe_payload(sqlite_detail)

    try:
        for rec in _chroma_backtest_records():
            if rec.get("id") == result_id:
                return json_safe_payload(_normalize_backtest_detail(rec))
    except Exception:
        pass  # ChromaDB may be unavailable; fall through to other sources.
    file_backed = _build_file_only_backtest_detail(result_id)
    if file_backed:
        return json_safe_payload(file_backed)
    if not remote_skip:
        remote_detail = _fetch_remote_backtest_detail(result_id, log_errors=True)
        if remote_detail:
            return json_safe_payload(remote_detail)
    raise HTTPException(status_code=404, detail="result not found")


def get_backtest_chart_context(result_id: str, remote_skip: bool = False):
    sqlite_detail = _build_sqlite_backtest_detail(result_id)
    if sqlite_detail:
        sqlite_artifact = _load_backtest_chart_artifact(
            result_id,
            sqlite_detail.get("config", {}) if isinstance(sqlite_detail.get("config"), dict) else {},
            str(sqlite_detail.get("result_type") or "backtest"),
        )
        if sqlite_artifact:
            sqlite_artifact["result_id"] = result_id
            sqlite_artifact["source"] = "artifact"
            sqlite_artifact.pop("source_path", None)
            return json_safe_payload(sqlite_artifact)

    detail = sqlite_detail or get_backtest_result(result_id, remote_skip=remote_skip)
    config = detail.get("config") if isinstance(detail.get("config"), dict) else {}
    result_type = str(detail.get("result_type") or "backtest")

    artifact = _load_backtest_chart_artifact(result_id, config, result_type)
    if artifact:
        artifact["result_id"] = result_id
        artifact["source"] = "artifact"
        artifact.pop("source_path", None)
        return json_safe_payload(artifact)

    try:
        from forven.strategies import backtest as backtest_mod

        detail["_allow_remote_fallback"] = True
        payload = backtest_mod.build_backtest_chart_context_from_result_detail(detail)
    except Exception as exc:
        log.exception("Failed to build backtest chart context")
        raise HTTPException(status_code=500, detail=f"Failed to build chart context: {exc}") from exc

    normalized = _normalize_backtest_chart_context_payload(payload)
    if normalized is None:
        raise HTTPException(status_code=500, detail="Invalid chart context payload generated")
    normalized["result_id"] = result_id
    normalized["source"] = "recomputed"
    return json_safe_payload(normalized)


def trash_backtest_result(result_id: str):
    """Move a result into trash."""
    with get_db() as conn:
        _set_backtest_result_trash(conn, result_id, deleted=True)
    return {"status": "ok", "id": result_id}


def recover_backtest_result(result_id: str):
    """Restore a trashed result."""
    with get_db() as conn:
        _set_backtest_result_trash(conn, result_id, deleted=False)
    return {"status": "ok", "id": result_id}


def permanent_delete_backtest_result(result_id: str):
    """Permanently remove a result from trash view."""
    with get_db() as conn:
        conn.execute("DELETE FROM backtest_result_trash WHERE result_id = ?", (result_id,))
        _set_backtest_result_trash(conn, result_id, deleted=False)
    return {"status": "ok", "id": result_id}


def get_backtest_trash(limit: int = 200):
    """Get trashed results for UI restore operations."""
    records = _chroma_backtest_records()
    with get_db() as conn:
        deleted = _get_backtest_result_deleted_ids(conn)

    summary = {}
    for rec in records:
        rid = rec.get("id")
        if rid not in deleted:
            continue
        summary[rid] = _normalize_backtest_summary(rec)

    now = datetime.now(timezone.utc).timestamp() if "datetime" in globals() else None
    out = []
    for rid, row in list(summary.items())[:limit]:
        deleted_at = None
        with get_db() as conn:
            row_data = conn.execute(
                "SELECT deleted_at FROM backtest_result_trash WHERE result_id = ?",
                (rid,),
            ).fetchone()
        if row_data and row_data["deleted_at"]:
            deleted_at = row_data["deleted_at"]
        days = 0
        if deleted_at and now is not None:
            try:
                ts = datetime.fromisoformat(deleted_at.replace("Z", "+00:00")).timestamp()
                days = max(0, 30 - int((now - ts) / 86400))
            except Exception:
                days = 0
        out.append({
            "id": row["id"],
            "job_id": row["job_id"],
            "strategy_name": row["strategy_name"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "created_at": row["created_at"],
            "deleted_at": deleted_at or _now(),
            "days_until_purge": days,
            "total_return": row["total_return"],
            "annualized_return_pct": row.get("annualized_return_pct"),
            "sharpe_ratio": row["sharpe_ratio"],
        })
    return out


def batch_delete_results(payload: dict):
    """Batch move results into trash."""
    ids = payload.get("ids", [])
    if not isinstance(ids, list):
        return {"status": "error", "error": "ids must be a list"}
    with get_db() as conn:
        for rid in ids:
            if isinstance(rid, str):
                _set_backtest_result_trash(conn, rid.strip(), deleted=True)
    return {"status": "ok", "count": len(ids)}


def batch_recover_results(payload: dict):
    """Batch restore results from trash."""
    ids = payload.get("ids", [])
    if not isinstance(ids, list):
        return {"status": "error", "error": "ids must be a list"}
    with get_db() as conn:
        for rid in ids:
            if isinstance(rid, str):
                _set_backtest_result_trash(conn, rid.strip(), deleted=False)
    return {"status": "ok", "count": len(ids)}


def empty_backtest_trash():
    """Empty all trashed items from UI view."""
    with get_db() as conn:
        conn.execute("DELETE FROM backtest_result_trash")
    return {"status": "ok", "count": 0}


def get_backtesting_status(remote_skip: bool = False):
    """Backtesting status from local storage (non-blocking)."""
    remote_enabled, remote_base = _resolve_remote_backtesting_mode()
    runs_payload = get_backtesting_runs(limit=10)
    runs = runs_payload.get("runs", []) if isinstance(runs_payload, dict) else []
    outcomes_payload = get_backtesting_outcomes()
    outcomes = outcomes_payload if isinstance(outcomes_payload, dict) else {}
    remote_base_url = _resolve_backtest_results_remote_api()
    remote_available = False
    remote_error = None
    if remote_base_url and not remote_skip:
        remote_available = _is_remote_backtest_results_available()
        if not remote_available:
            remote_error = f"Remote backtesting host unreachable: {remote_base_url}"
    result = {
        "available": True,
        "base_url": remote_base_url,
        "remote_available": remote_available,
        "runs": runs,
        "outcomes": outcomes,
    }
    if remote_error:
        result["remote_error"] = remote_error
    return result


def get_evolution():
    """Strategy counts grouped by lifecycle status."""
    # FIX: Only load paper and live_graduated strategies
    strats = get_strategies(status='paper') + get_strategies(status='live_graduated')
    counts = {}
    for s in strats:
        st = s.get("stage", "unknown")
        counts[st] = counts.get(st, 0) + 1
    
    # Compatibility aliases for frontend telemetry blocks
    counts["researching"] = counts.get("quick_screen", 0)
    counts["backtesting"] = counts.get("gauntlet", 0)
    counts["paper_trading"] = counts.get("paper", 0)
    counts["deployed"] = counts.get("live_graduated", 0)
    
    return counts


# â”€â”€ Phase 2: Forven backtesting endpoints â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_backtesting_runs(limit: int = 20):
    """List recent backtest runs."""
    try:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT run_id, strategy_id, is_metrics_json, oos_metrics_json, robustness_score, timestamp "
                "FROM backtest_runs ORDER BY datetime(timestamp) DESC LIMIT ?",
                (max(int(limit), 1),),
            ).fetchall()
        runs = []
        for row in rows:
            item = dict(row)
            runs.append(
                {
                    "id": item.get("run_id"),
                    "run_id": item.get("run_id"),
                    "strategy_id": item.get("strategy_id"),
                    "status": "completed",
                    "created_at": item.get("timestamp"),
                    "completed_at": item.get("timestamp"),
                    "metrics": {
                        "in_sample": _parse_json_blob(item.get("is_metrics_json"), {}),
                        "out_of_sample": _parse_json_blob(item.get("oos_metrics_json"), {}),
                        "robustness": item.get("robustness_score"),
                    },
                }
            )
        return json_safe_payload({"runs": runs})
    except Exception as e:
        return {"error": str(e), "runs": []}


def get_backtesting_outcomes():
    """Get aggregate strategy outcomes from locally stored results."""
    records = _chroma_backtest_records()
    if not records:
        return {
            "total_results": 0,
            "wins": 0,
            "losses": 0,
            "win_rate_pct": 0.0,
            "avg_total_return_pct": 0.0,
            "avg_sharpe": 0.0,
        }

    wins = 0
    losses = 0
    total_returns: list[float] = []
    sharpe_values: list[float] = []
    for rec in records:
        meta = rec.get("metadata") or {}
        total_return = _coerce_legacy_metadata_float(meta.get("total_return"))
        sharpe = _coerce_legacy_metadata_float(meta.get("sharpe"))
        total_returns.append(total_return)
        sharpe_values.append(sharpe)
        if total_return >= 0:
            wins += 1
        else:
            losses += 1

    total = len(records)
    avg_return = sum(total_returns) / total if total else 0.0
    avg_sharpe = sum(sharpe_values) / total if total else 0.0
    win_rate = (wins / total) * 100.0 if total else 0.0
    return {
        "total_results": total,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 2),
        "avg_total_return_pct": round(avg_return, 4),
        "avg_sharpe": round(avg_sharpe, 4),
    }


def get_backtesting_prompt_packs():
    """Get available prompt pack names (local fallback)."""
    return {
        "default": "default",
        "packs": {
            "default": {
                "name": "default",
                "description": "Balanced strategy discovery and validation.",
            },
            "conservative": {
                "name": "conservative",
                "description": "Risk-first filtering with tighter drawdown limits.",
            },
            "aggressive": {
                "name": "aggressive",
                "description": "Higher exploration for alpha discovery.",
            },
        },
    }


def _normalize_strategy_lookup_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _extract_strategy_suffix_token(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    direct = re.fullmatch(r"([A-Za-z])(\d{4,5})", text)
    if direct:
        return direct.group(2)
    embedded = re.search(r"\b[A-Za-z](\d{4,5})\b", text)
    if embedded:
        return embedded.group(1)
    return None


def _extract_base_asset_symbol(value: object, fallback: object = None) -> str:
    raw = str(value or fallback or "").strip().upper()
    if not raw:
        return "BTC"
    for sep in ("/", "-", "_"):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
            break
    for suffix in ("PERP", "USDT", "USDC", "USD"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.strip() or "BTC"


def _infer_strategy_type_from_name(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    if "bollinger" in normalized or re.search(r"\bbb\b", normalized):
        return "bollinger"
    if "keltner" in normalized or re.search(r"\bkc\b", normalized):
        return "keltner"
    if "orb" in normalized or "opening range" in normalized:
        return "orb"
    if "macd" in normalized:
        return "macd"
    if "ema" in normalized and ("cross" in normalized or "crossover" in normalized):
        return "ema_cross"
    if "rsi" in normalized:
        return "rsi_momentum"
    if "stoch" in normalized:
        return "stochastic"
    return None


def _parse_strategy_params_blob(value: object) -> dict:
    parsed = _safe_json(value)
    return parsed if isinstance(parsed, dict) else {}


def _timeframe_to_minutes(value: object) -> int:
    raw = str(value or "1h").strip()
    match = re.fullmatch(r"(\d+)([mhdwM])", raw)
    if not match:
        return 60
    qty = max(int(match.group(1)), 1)
    unit = match.group(2)
    unit_map = {
        "m": 1,
        "h": 60,
        "d": 1440,
        "w": 10080,
        "M": 43200,
    }
    return qty * int(unit_map.get(unit, 60))


def _to_ratio(value: object, default: float = 1.0) -> float:
    """Normalize ratio-like values, accepting either fractions or percent points."""
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    if parsed < 0:
        parsed = 0.0
    if parsed > 1.0:
        parsed = parsed / 100.0
    if parsed > 1.0:
        parsed = 1.0
    return float(parsed)


def _to_percent_points(value: object, default: float = 0.0) -> float:
    """Normalize percent-like values, accepting either fractions or percent points."""
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    if abs(parsed) <= 1.0:
        parsed = parsed * 100.0
    return float(parsed)


def _should_auto_trash_backtest_result(
    *,
    total_return_pct: float,
    sharpe: float,
    max_drawdown_ratio: float,
    total_trades: int,
) -> tuple[bool, str]:
    """Return True when a result should be auto-hidden from active backtest file manager views."""
    total_return_points = _to_percent_points(total_return_pct, 0.0)
    if int(total_trades) <= 0:
        return True, "no_closed_trades"
    if float(total_return_points) <= 0.0:
        return True, f"non_positive_return:{float(total_return_points):.4f}"

    try:
        from forven.policy import load_pipeline_config

        config = load_pipeline_config()
        gate = config.get("quick_screen", {}) if isinstance(config, dict) else {}
        if not isinstance(gate, dict):
            gate = {}

        min_return = _to_percent_points(gate.get("min_total_return_pct", 5.0), 5.0)
        min_sharpe = float(gate.get("min_sharpe", 1.0))
        max_dd_limit = _to_ratio(gate.get("max_drawdown_pct", 0.25), 0.25)
        dd = _to_ratio(max_drawdown_ratio, 1.0)

        if float(total_return_points) <= min_return:
            return True, f"return_below_quick_screen:{float(total_return_points):.4f}<={min_return:.4f}"
        if float(sharpe) <= min_sharpe:
            return True, f"sharpe_below_quick_screen:{float(sharpe):.4f}<={min_sharpe:.4f}"
        if dd >= max_dd_limit:
            return True, f"drawdown_above_quick_screen:{dd:.6f}>={max_dd_limit:.6f}"
    except Exception as exc:
        log.warning("Backtest auto-trash gate evaluation failed; using hard return checks: %s", exc)

    return False, ""


# Each automated pipeline stage that runs a backtest maps to its own window setting.
# stage_backtest_duration_days() resolves the effective window for a stage, treating a
# stored value of 0/blank as "inherit the global Default backtest window".
_STAGE_DURATION_SETTING_KEYS = {
    "quick_screen": "quick_screen_duration_days",
    "timeframe_sweep": "timeframe_sweep_duration_days",
    "optimization": "optimization_duration_days",
    "confirmation": "confirmation_duration_days",
    "walk_forward": "walk_forward_duration_days",
    "cost_stress": "cost_stress_duration_days",
    "evolution": "evolution_duration_days",
}


def stage_backtest_duration_days(stage_key: str, settings: dict | None = None) -> int:
    """Resolve the backtest window (calendar days) for a pipeline STAGE.

    Each stage has its own tunable knob (e.g. quick_screen_duration_days). A stored
    value of 0 / blank means "inherit the global Default backtest window"
    (backtest_duration_days). Always returns a positive int.
    """
    s = settings if isinstance(settings, dict) else get_settings()
    try:
        global_default = int(
            s.get("backtest_duration_days", DEFAULT_BACKTEST_DURATION_DAYS)
            or DEFAULT_BACKTEST_DURATION_DAYS
        )
    except (TypeError, ValueError):
        global_default = DEFAULT_BACKTEST_DURATION_DAYS
    key = _STAGE_DURATION_SETTING_KEYS.get(str(stage_key or "").strip())
    if key is not None:
        raw = s.get(key)
        if raw not in (None, "", 0, "0"):
            try:
                stage_val = int(raw)
            except (TypeError, ValueError):
                stage_val = 0
            if stage_val > 0:
                return stage_val
    return max(1, global_default)


def _estimate_backtest_bars(
    start: str | None,
    end: str | None,
    timeframe: str | None,
    duration_days_override: int | None = None,
) -> int:
    settings = get_settings()
    if duration_days_override and int(duration_days_override) > 0:
        duration_days = int(duration_days_override)
    else:
        duration_days = int(settings.get("backtest_duration_days", DEFAULT_BACKTEST_DURATION_DAYS) or DEFAULT_BACKTEST_DURATION_DAYS)
    minutes_per_bar = max(_timeframe_to_minutes(timeframe), 1)
    default_bars = (duration_days * 24 * 60) // minutes_per_bar

    if not start or not end:
        return max(220, default_bars)
    try:
        start_dt = datetime.fromisoformat(str(start).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except Exception:
        return max(220, default_bars)
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    delta_seconds = (end_dt - start_dt).total_seconds()
    if delta_seconds <= 0:
        return max(220, default_bars)
    estimated = int(delta_seconds / float(minutes_per_bar * 60)) + 2
    return max(220, min(estimated, 100_000))


def _get_strategy_row_by_id(strategy_id: str) -> dict | None:
    target = str(strategy_id or "").strip()
    if not target:
        return None
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT * FROM strategies
            WHERE LOWER(TRIM(id)) = LOWER(TRIM(?))
               OR LOWER(TRIM(COALESCE(display_id, ''))) = LOWER(TRIM(?))
               OR LOWER(TRIM(COALESCE(name, ''))) = LOWER(TRIM(?))
            ORDER BY CASE
                WHEN LOWER(TRIM(id)) = LOWER(TRIM(?)) THEN 0
                WHEN LOWER(TRIM(COALESCE(display_id, ''))) = LOWER(TRIM(?)) THEN 1
                ELSE 2
            END
            LIMIT 1
            """,
            (target, target, target, target, target),
        ).fetchone()
    if row:
        return dict(row)

    fallback = _resolve_strategy_for_backtest(target)
    return dict(fallback) if fallback else None


def _require_existing_strategy_row(strategy_id: str) -> dict:
    row = _get_strategy_row_by_id(strategy_id)
    if row:
        return row
    normalized = str(strategy_id or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail="strategy_id is required")
    # Fallback: check if strategy_id matches a prebuilt type in the registry.
    # Persist a minimal row so downstream artifacts (e.g. backtest_results) can
    # satisfy their FK on strategies(id).
    try:
        from forven.strategies.registry import _TYPE_MAP, discover
        discover()
        # The id may be a registered type directly, OR a per-run scratch id of the
        # form "<type>__<suffix>" (e.g. rule_engine__<spechash>) minted so distinct
        # ad-hoc visual strategies don't all collide under the bare type. Resolve
        # the runtime type from the prefix in that case.
        runtime_type: str | None = None
        if normalized in _TYPE_MAP:
            runtime_type = normalized
        elif "__" in normalized:
            prefix = normalized.split("__", 1)[0]
            if prefix in _TYPE_MAP:
                runtime_type = prefix
        if runtime_type:
            import json as _json
            cls = _TYPE_MAP[runtime_type]
            instance = cls(normalized, {})
            params_json = _json.dumps(instance.default_params)
            now = _now()
            is_adhoc = runtime_type != normalized
            source = "manual_adhoc" if is_adhoc else "prebuilt"
            with get_db() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO strategies (
                        id, name, type, runtime_type, params,
                        symbol, timeframe, status, stage, owner,
                        source, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prebuilt', 'prebuilt', 'system', ?, ?, ?)
                    """,
                    (
                        normalized,
                        instance.name,
                        runtime_type,
                        runtime_type,
                        params_json,
                        instance.asset,
                        "1h",
                        source,
                        now,
                        now,
                    ),
                )
            persisted = _get_strategy_row_by_id(normalized)
            if persisted:
                return persisted
            return {
                "id": normalized,
                "name": instance.name,
                "type": runtime_type,
                "runtime_type": runtime_type,
                "params": params_json,
                "symbol": instance.asset,
                "timeframe": "1h",
                "status": "prebuilt",
            }
    except Exception:
        pass
    raise HTTPException(status_code=404, detail=f"strategy not found: {normalized}")


def _resolve_strategy_for_backtest(
    strategy_name: str,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> dict | None:
    target = str(strategy_name or "").strip()
    if not target:
        return None
    target_lower = target.lower()
    target_key = _normalize_strategy_lookup_key(target)
    target_suffix = _extract_strategy_suffix_token(target)
    desired_symbol = _extract_base_asset_symbol(symbol) if symbol else ""
    desired_timeframe = str(timeframe or "").strip().lower()

    best_row: dict | None = None
    best_score = -1
    # FIX: Only load paper and live_graduated strategies
    for row in get_strategies(status='paper') + get_strategies(status='live_graduated'):
        row_id = str(row.get("id") or "").strip()
        row_name = str(row.get("name") or "").strip()
        row_display = str(row.get("display_id") or "").strip()
        if not row_id and not row_name:
            continue

        row_id_lower = row_id.lower()
        row_name_lower = row_name.lower()
        row_display_lower = row_display.lower()
        row_id_key = _normalize_strategy_lookup_key(row_id)
        row_name_key = _normalize_strategy_lookup_key(row_name)

        score = 0
        if target_lower and target_lower == row_id_lower:
            score = max(score, 200)
        if target_lower and target_lower == row_display_lower:
            score = max(score, 195)
        if target_lower and target_lower == row_name_lower:
            score = max(score, 190)
        if target_key and target_key == row_id_key:
            score = max(score, 185)
        if target_key and target_key == row_name_key:
            score = max(score, 180)
        if target_lower and target_lower in row_id_lower:
            score = max(score, 170)
        if target_lower and target_lower in row_name_lower:
            score = max(score, 160)

        row_suffix = _extract_strategy_suffix_token(row_id) or _extract_strategy_suffix_token(row_display) or _extract_strategy_suffix_token(row_name)
        if target_suffix and row_suffix and target_suffix == row_suffix:
            score = max(score, 188)

        if desired_symbol:
            row_symbol = _extract_base_asset_symbol(row.get("symbol"))
            if row_symbol and row_symbol == desired_symbol:
                score += 4
        if desired_timeframe:
            row_tf = str(row.get("timeframe") or "").strip().lower()
            if row_tf and row_tf == desired_timeframe:
                score += 2

        if score > best_score:
            best_score = score
            best_row = row

    if best_score < 150:
        return None
    return best_row


def _normalize_strategy_type(value: object) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    from forven.strategies.sandbox_proxy import is_sandbox_only_type

    # Namespaced sandbox types are exact worker-registry keys: case-sensitive and
    # never family-aliased. Lowercasing one breaks the worker lookup for a
    # mixed-case imported module (silent 0-signal runs), and the family alias /
    # *_orb collapse below would execute the WRONG builtin class for an imported
    # strategy whose module name ends in a family token.
    if is_sandbox_only_type(raw):
        return raw
    normalized = raw.lower()
    aliases = {
        "bb": "bollinger",
        "bollinger_band": "bollinger",
        "bollinger-band": "bollinger",
        "bollinger_bands": "bollinger",
        "bollinger-bands": "bollinger",
        "kc": "keltner",
        "keltner_channel": "keltner",
        "keltner-channel": "keltner",
        "ema": "ema_cross",
        "ema-cross": "ema_cross",
        "ema crossover": "ema_cross",
        "rsi": "rsi_momentum",
        "stoch": "stochastic",
        "funding_rate": "funding",
        "funding-rate": "funding",
        "opening_range_breakout": "orb",
        "opening-range-breakout": "orb",
    }
    normalized = aliases.get(normalized, normalized)
    if normalized.endswith("_orb") or normalized.endswith("-orb"):
        return "orb"
    if normalized in {
        "backtest",
        "backtesting",
        "strategy",
        "generic",
        "scan",
        "manual",
        "autopilot",
        "campaign",
        "code",
        "core",
    }:
        return None
    return normalized


def _collect_strategy_type_markers(payload: object, max_items: int = 400) -> list[str]:
    markers: list[str] = []
    stack: list[object] = [payload]
    while stack and len(markers) < max_items:
        current = stack.pop()
        if current is None:
            continue
        if isinstance(current, dict):
            for key, value in current.items():
                key_text = str(key or "").strip().lower()
                if key_text:
                    markers.append(key_text)
                stack.append(value)
            continue
        if isinstance(current, (list, tuple, set)):
            stack.extend(list(current))
            continue
        if isinstance(current, str):
            text = current.strip().lower()
            if text:
                markers.append(text)
    return markers


def _infer_strategy_type_from_payload(payload: object) -> str | None:
    if isinstance(payload, dict):
        lowered_keys = {str(k or "").strip().lower() for k in payload.keys()}
        if (
            {"fast", "slow", "signal"}.issubset(lowered_keys)
            or "macd_fast" in lowered_keys
            or "macd_slow" in lowered_keys
            or "macd_signal" in lowered_keys
        ):
            return "macd"
        if "bb_period" in lowered_keys or "bb_std" in lowered_keys:
            return "bollinger"
        if "kc_period" in lowered_keys or "kc_mult" in lowered_keys:
            return "keltner"
        if "rsi_period" in lowered_keys or "rsi_entry" in lowered_keys or "rsi_exit" in lowered_keys:
            return "rsi_momentum"
        if "stoch_k" in lowered_keys or "stoch_d" in lowered_keys or "k_period" in lowered_keys:
            return "stochastic"
        if "donchian" in lowered_keys or "donchian_upper" in lowered_keys or "donchian_lower" in lowered_keys:
            return "donchian"
        if "range_bars" in lowered_keys or "orb" in lowered_keys:
            return "orb"
        if "ema_fast" in lowered_keys and "ema_slow" in lowered_keys:
            return "ema_cross"

    markers = _collect_strategy_type_markers(payload)
    joined = " ".join(markers)
    if "macd" in joined:
        return "macd"
    if "bollinger" in joined or re.search(r"\bbb\b", joined):
        return "bollinger"
    if "keltner" in joined or re.search(r"\bkc\b", joined):
        return "keltner"
    if "orb" in joined or "opening range" in joined:
        return "orb"
    if "stoch" in joined:
        return "stochastic"
    if "rsi" in joined:
        return "rsi_momentum"
    if "ema" in joined and ("cross" in joined or "crossover" in joined):
        return "ema_cross"
    return None


def _resolve_backtesting_strategy_type(
    *,
    explicit_type: object = None,
    strategy_name: object = None,
    params: object = None,
    payload: object = None,
) -> str | None:
    resolved = _normalize_strategy_type(explicit_type)
    if resolved:
        return resolved
    for candidate in (params, payload):
        inferred = _infer_strategy_type_from_payload(candidate)
        normalized_inferred = _normalize_strategy_type(inferred)
        if normalized_inferred:
            return normalized_inferred
    return _normalize_strategy_type(_infer_strategy_type_from_name(strategy_name))


def resolve_execution_strategy_type(strategy_row: object) -> str | None:
    """Return the column that names the EXECUTABLE type for a strategies row.

    Dropzone/imported strategies carry a bare ``type`` (the author's TYPE_NAME)
    plus a namespaced ``runtime_type`` (``imported__<module>``) that routes
    execution through the sandbox worker. Execution entry points must resolve
    the runtime_type: the bare type's source file was moved to ``imported/`` at
    registration, so resolving it scans ``custom/`` and lands on the orphan
    guard.
    """
    from forven.strategies.sandbox_proxy import is_sandbox_only_type

    if strategy_row is None:
        return None

    def _column(key: str) -> object:
        try:
            if isinstance(strategy_row, dict):
                return strategy_row.get(key)
            return strategy_row[key]
        except (IndexError, KeyError, TypeError):
            return None

    runtime_type = str(_column("runtime_type") or "").strip()
    if runtime_type and is_sandbox_only_type(runtime_type):
        return runtime_type
    bare = _column("type")
    text = str(bare).strip() if bare is not None else ""
    return text or None


def _infer_strategy_context_from_task_audit(strategy_id: str) -> dict | None:
    target = str(strategy_id or "").strip()
    if not target:
        return None

    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT tal.tool_name, tal.input_json, tal.output_summary
                FROM task_audit_log tal
                JOIN agent_tasks at
                  ON (
                        LOWER(TRIM(COALESCE(at.display_id, ''))) = LOWER(TRIM(tal.task_id))
                     OR TRIM(CAST(at.id AS TEXT)) = TRIM(tal.task_id)
                  )
                WHERE LOWER(TRIM(COALESCE(at.strategy_id, ''))) = LOWER(TRIM(?))
                  AND LOWER(TRIM(COALESCE(tal.tool_name, ''))) IN ('run_backtest', 'optimize_strategy')
                ORDER BY tal.id DESC
                LIMIT 200
                """,
                (target,),
            ).fetchall()
    except Exception:
        return None

    best_context: dict | None = None
    best_score = -1
    for row in rows:
        payload = _parse_json_blob(row["input_json"], {})
        if not isinstance(payload, dict):
            continue
        params = _parse_strategy_params_blob(payload.get("params"))
        strategy_type = _resolve_backtesting_strategy_type(
            explicit_type=payload.get("strategy_type"),
            strategy_name=payload.get("strategy") or payload.get("strategy_id") or target,
            params=params,
            payload=payload,
        )
        if not strategy_type:
            continue

        tool_name = str(row["tool_name"] or "").strip().lower()
        output_summary = str(row["output_summary"] or "").strip().lower()
        has_error = bool(output_summary) and "error" in output_summary
        score = 0
        if not has_error:
            score += 100
        if tool_name == "run_backtest":
            score += 20
        if params:
            score += 10

        if score > best_score:
            best_score = score
            best_context = {
                "strategy_type": strategy_type,
                "params": params,
                "symbol": _extract_base_asset_symbol(payload.get("asset"), payload.get("symbol")),
                "timeframe": str(payload.get("timeframe") or "").strip() or None,
                "from_tool": tool_name or None,
                "from_success": not has_error,
            }
            if score >= 130:
                break

    return best_context


def _backfill_strategy_type_from_context(
    strategy_id: str,
    strategy_row: dict,
    inferred_type: str | None,
    inferred_params: dict | None,
) -> None:
    current_type = _normalize_strategy_type((strategy_row or {}).get("type"))
    current_params = _parse_strategy_params_blob((strategy_row or {}).get("params"))
    next_type = inferred_type if (not current_type and inferred_type) else None
    next_params = inferred_params if (not current_params and isinstance(inferred_params, dict) and inferred_params) else None
    next_name: str | None = None
    if next_type:
        current_name = str((strategy_row or {}).get("name") or "").strip()
        legacy_tokens = {
            "-SCAN-",
            "-MANUAL-",
            "-AUTOPILOT-",
            "-CAMPAIGN-",
            "-CODE-",
            "-CORE-",
            "-GENERIC-",
            "-STRATEGY-",
            "-BACKTEST-",
            "-BACKTESTING-",
        }
        if (not current_name) or any(token in current_name.upper() for token in legacy_tokens):
            next_name = build_strategy_container_name(
                symbol=(strategy_row or {}).get("symbol"),
                type_=next_type,
                strategy_id=strategy_id,
            )
    if not next_type and not next_params and not next_name:
        return

    with get_db() as conn:
        conn.execute(
            """
            UPDATE strategies
            SET type = COALESCE(?, type),
                params = COALESCE(?, params),
                name = COALESCE(?, name),
                updated_at = ?
            WHERE id = ?
            """,
            (
                next_type,
                json.dumps(next_params) if next_params else None,
                next_name,
                _now(),
                strategy_id,
            ),
        )


def _persist_completed_backtest_run(
    *,
    strategy_id: str,
    strategy_name: str,
    strategy_type: str,
    asset: str,
    timeframe: str,
    params: dict | None,
    run: dict,
    start: str | None = None,
    end: str | None = None,
    definition_json: dict | None = None,
    initial_capital: float | None = None,
    fee_bps: float | None = None,
    slippage_bps: float | None = None,
    trade_mode: str | None = None,
    allow_shorting: bool | None = None,
    stop_loss_pct: float | None = None,
    take_profit_pct: float | None = None,
    trailing_stop_pct: float | None = None,
    time_stop_bars: int | None = None,
    sizing_mode: str | None = None,
    fixed_size: float | None = None,
    risk_per_trade: float | None = None,
    atr_stop_multiplier: float | None = None,
    kelly_multiplier: float | None = None,
    kelly_lookback: int | None = None,
    leverage: float | None = None,
    lifecycle_id: str | None = None,
    session_id: str | None = None,
    as_of: str | None = None,
) -> dict[str, object]:
    metrics = run.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    oos_metrics = metrics.get("out_of_sample")
    if not isinstance(oos_metrics, dict):
        oos_metrics = {}

    total_return_pct = _coerce_legacy_metadata_float(metrics.get("total_return_pct"), 0.0)
    sharpe = _coerce_legacy_metadata_float(metrics.get("sharpe"), 0.0)
    max_drawdown = _coerce_legacy_metadata_float(metrics.get("max_drawdown_pct"), 0.0)
    total_trades = int(_coerce_legacy_metadata_float(metrics.get("total_trades"), 0.0) or 0)

    evaluation_monthly_return = _coerce_optional_float(oos_metrics.get("monthly_return_pct"))
    evaluation_annualized_return = _coerce_optional_float(oos_metrics.get("annualized_return_pct"))
    evaluation_backtest_months = _coerce_optional_float(oos_metrics.get("backtest_months"))

    full_backtest_months = _coerce_optional_float(metrics.get("lookback_months"))
    if full_backtest_months is None:
        full_backtest_months = _coerce_optional_float(metrics.get("backtest_months"))
    if full_backtest_months is None:
        full_backtest_months = evaluation_backtest_months

    settings = get_settings()
    now_iso = _now()
    submit_start = str(start or run.get("start_date") or "").strip()
    submit_end = str(end or run.get("end_date") or "").strip()
    if not submit_end:
        submit_end = now_iso
    if not submit_start:
        try:
            duration_days = int(settings.get("backtest_duration_days", DEFAULT_BACKTEST_DURATION_DAYS) or DEFAULT_BACKTEST_DURATION_DAYS)
        except Exception:
            duration_days = DEFAULT_BACKTEST_DURATION_DAYS
        try:
            submit_end_dt = datetime.fromisoformat(submit_end.replace("Z", "+00:00"))
        except Exception:
            submit_end_dt = datetime.now(timezone.utc)
        if submit_end_dt.tzinfo is None:
            submit_end_dt = submit_end_dt.replace(tzinfo=timezone.utc)
        submit_start = (submit_end_dt - timedelta(days=max(duration_days, 1))).isoformat()

    safe_asset_token = re.sub(r"[^a-z0-9]+", "-", str(asset or "").strip().lower()).strip("-")
    if not safe_asset_token:
        safe_asset_token = _extract_base_asset_symbol(asset).lower() or "asset"
    job_id = f"bt_{uuid4().hex[:12]}"
    result_id = f"{strategy_id}-{safe_asset_token}-{int(time.time() * 1000)}"

    config_payload: dict[str, object] = {
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "strategy": strategy_id,
        "strategy_type": strategy_type,
        "symbol": asset,
        "timeframe": timeframe,
        "start": submit_start,
        "end": submit_end,
        "params": params if isinstance(params, dict) else {},
        "definition_json": definition_json if isinstance(definition_json, dict) else None,
        "initial_capital": initial_capital,
        "fee_bps": fee_bps,
        "slippage_bps": slippage_bps,
        "trade_mode": trade_mode or run.get("trade_mode"),
        "position_model": run.get("position_model"),
        "allow_shorting": allow_shorting,
        "stop_loss_pct": stop_loss_pct,
        "take_profit_pct": take_profit_pct,
        "trailing_stop_pct": trailing_stop_pct,
        "time_stop_bars": time_stop_bars,
        "sizing_mode": sizing_mode,
        "fixed_size": fixed_size,
        "risk_per_trade": risk_per_trade,
        "atr_stop_multiplier": atr_stop_multiplier,
        "kelly_multiplier": kelly_multiplier,
        "kelly_lookback": kelly_lookback,
        "leverage": leverage,
        "job_id": job_id,
        "dropzone_session_id": (str(session_id).strip() or None) if session_id else None,
    }
    # Verdict auditability (edge-data-expansion Run 2): record the identity of
    # the data this result was scored on (checksum/rows/span/market/as_of).
    # Drift — rebuilds, venue changes, restatements — becomes DETECTABLE by
    # comparing fingerprints instead of remembered by operators.
    try:
        from forven.dataeng.quality_gate import dataset_fingerprint

        config_payload["data_fingerprint"] = dataset_fingerprint(asset, timeframe, as_of=as_of)
    except Exception:
        pass
    compact_config = {k: v for k, v in config_payload.items() if v is not None}
    # NOTE: `lifecycle_id` now has no consumer in this function. Its only reader
    # was `store_backtest_result(lifecycle_strategy_id=...)` in the ChromaDB
    # memory layer, deleted in 97ac259b; the replacement
    # `record_backtest_for_learning` takes no lifecycle id. The parameter is kept
    # because callers still pass it — do not "fix" it by inventing a new
    # consumer; give it one deliberately or drop it from the signature.

    metrics_for_storage = dict(metrics)
    if full_backtest_months is not None:
        metrics_for_storage["backtest_months"] = float(full_backtest_months)
    if evaluation_monthly_return is not None:
        metrics_for_storage["evaluation_monthly_return_pct"] = float(evaluation_monthly_return)
    if evaluation_annualized_return is not None:
        metrics_for_storage["evaluation_annualized_return_pct"] = float(evaluation_annualized_return)
    if evaluation_backtest_months is not None:
        metrics_for_storage["evaluation_backtest_months"] = float(evaluation_backtest_months)

    actual_start = str(run.get("start_date") or compact_config.get("start") or "").strip()
    actual_end = str(run.get("end_date") or compact_config.get("end") or "").strip()

    _persist_backtest_result_row(
        result_id=result_id,
        strategy_id=strategy_id,
        result_type="backtest",
        symbol=asset,
        timeframe=timeframe,
        start_date=actual_start,
        end_date=actual_end,
        metrics=metrics_for_storage,
        config=compact_config,
        created_at=now_iso,
    )

    if compact_config.get("dropzone_session_id"):
        from forven.ai_dropzone_sessions import touch_session

        touch_session(compact_config["dropzone_session_id"])

    try:
        from forven.quant_skills_extractor import record_backtest_for_learning

        record_backtest_for_learning(
            strategy_id=strategy_id,
            asset=asset,
            strategy_type=str(strategy_type),
            params=params if isinstance(params, dict) else {},
            metrics=metrics_for_storage,
            fitness=float(sharpe),
            strategy_name=strategy_name,
            config=compact_config,
        )
    except Exception:
        pass

    try:
        _write_backtest_result_artifacts(
            result_id, job_id, run.get("trades"),
            equity_curve=run.get("equity_curve"),
            benchmark_curve=run.get("benchmark_curve"),
            equity_curve_full=run.get("equity_curve_full"),
            benchmark_curve_full=run.get("benchmark_curve_full"),
        )
    except Exception:
        pass
    try:
        chart_context = _build_backtest_chart_context_payload(
            result_id=result_id,
            asset=asset,
            timeframe=timeframe,
            start_date=actual_start,
            end_date=actual_end,
            strategy_name=strategy_name,
            strategy_type=strategy_type,
            strategy_params=params if isinstance(params, dict) else {},
            trades=run.get("trades"),
            warnings=run.get("warnings") if isinstance(run.get("warnings"), list) else None,
        )
        if chart_context is not None:
            _write_backtest_chart_artifacts(result_id, job_id, chart_context)
    except Exception:
        pass

    try:
        auto_assign_best_symbol(strategy_id)
    except Exception:
        pass

    auto_trash, auto_reason = _should_auto_trash_backtest_result(
        total_return_pct=float(total_return_pct),
        sharpe=float(sharpe),
        max_drawdown_ratio=float(max_drawdown),
        total_trades=int(total_trades),
    )
    if auto_trash:
        with get_db() as conn:
            _set_backtest_result_trash(conn, result_id, deleted=True)
        log_activity(
            "warning",
            "simulation",
            f"Backtest auto-trashed for {strategy_id} ({asset} {timeframe})",
            {
                "job_id": job_id,
                "result_id": result_id,
                "reason": auto_reason,
                "total_return_pct": float(_to_percent_points(total_return_pct, 0.0)),
                "sharpe": float(sharpe),
                "max_drawdown_ratio": float(max_drawdown),
                "total_trades": int(total_trades),
            },
        )

    log_activity(
        "info",
        "simulation",
        f"Backtest submitted for {strategy_id} ({asset} {timeframe})",
        {"job_id": job_id, "result_id": result_id},
    )

    return {
        "job_id": job_id,
        "result_id": result_id,
        "metrics": metrics_for_storage,
    }


def post_backtest_preview(body: BacktestPreviewBody):
    """Real signal pre-flight: resolve the strategy the same way submit does,
    then run in-process signal generation over the chosen window and report
    entry/exit counts, density, data coverage and warnings — no persistence."""
    bars = _estimate_backtest_bars(body.start, body.end, body.timeframe)
    asset = _extract_base_asset_symbol(body.symbol)
    timeframe = str(body.timeframe or "1h").strip() or "1h"

    # Resolve strategy_type + base params (best-effort; preview must never 500).
    requested = str(body.strategy_name or "").strip()
    base_params: dict = {}
    explicit_type: str | None = None
    try:
        row = _require_existing_strategy_row(requested)
        if isinstance(row, dict):
            base_params = _parse_strategy_params_blob(row.get("params")) or {}
            explicit_type = resolve_execution_strategy_type(row)
            if not (asset and asset.strip()):
                asset = _extract_base_asset_symbol(str(row.get("symbol") or body.symbol))
    except Exception:
        row = None

    requested_params = body.params if isinstance(body.params, dict) else {}
    merged_params = {**base_params, **requested_params}
    strategy_definition_json = body.definition_json if isinstance(body.definition_json, dict) else None
    strategy_type = _resolve_backtesting_strategy_type(
        explicit_type=explicit_type,
        strategy_name=requested,
        params=merged_params,
        payload=strategy_definition_json,
    ) or requested

    try:
        from forven.strategies.backtest import preview_strategy_signals

        preview = preview_strategy_signals(
            asset=asset,
            strategy_type=strategy_type,
            params=merged_params,
            bars=bars,
            timeframe=timeframe,
            start_date=(str(body.start).strip() or None) if body.start else None,
            end_date=(str(body.end).strip() or None) if body.end else None,
            trade_mode=str(body.trade_mode or "long_only").strip() or "long_only",
        )
        return preview
    except Exception as exc:
        # Degrade to a data-coverage-only preview rather than failing the page.
        warnings: list[str] = [f"Signal preview unavailable: {exc}"]
        total_bars = 0
        try:
            from forven.strategies.backtest import load_backtest_candles

            frame = load_backtest_candles(asset=asset, bars=bars, timeframe=timeframe)
            total_bars = int(len(frame))
        except Exception:
            pass
        return {
            "total_bars": int(max(total_bars, 0)),
            "entry_count": 0, "exit_count": 0, "entry_pct": 0.0, "exit_pct": 0.0,
            "avg_bars_between_entries": None, "first_entry_bar": None, "last_entry_bar": None,
            "signal_density": "sparse", "warnings": warnings,
            "sample_entries": [], "sample_exits": [], "indicators": [],
        }


def post_backtest_preview_chart(body: PreviewChartBody) -> dict:
    """Live chart context (bars + indicator overlays + entry/exit markers) for a
    no-code rule_engine spec — computed in-process, never persisted. Powers the
    Strategy Creator's live preview chart. Never 500s; degrades to warnings."""
    asset = _extract_base_asset_symbol(body.symbol)
    timeframe = str(body.timeframe or "1h").strip() or "1h"
    spec = body.spec if isinstance(body.spec, dict) else {}
    try:
        from forven.strategies.backtest import build_strategy_preview_chart_context

        return build_strategy_preview_chart_context(
            asset=asset,
            timeframe=timeframe,
            start_date=(str(body.start).strip() or None) if body.start else None,
            end_date=(str(body.end).strip() or None) if body.end else None,
            spec=spec,
            trade_mode=str(body.trade_mode or "long_only").strip() or "long_only",
            strategy_name=str(body.name or "Visual strategy"),
        )
    except Exception as exc:  # noqa: BLE001 — preview must never break the page
        return {
            "bars": [], "entry_markers": [], "exit_markers": [],
            "main_indicators": [], "sub_indicators": [],
            "strategy_name": str(body.name or "Visual strategy"),
            "strategy_meta": "", "strategy_params": {"spec": spec},
            "warnings": [f"Preview chart unavailable: {exc}"],
        }


async def post_nl_to_spec(body: NlToSpecBody) -> dict:
    """Translate a natural-language strategy description into a rule_engine spec."""
    from forven.strategies.nl_spec_gen import nl_to_rule_spec

    return await nl_to_rule_spec(
        description=body.description,
        symbol=body.symbol,
        timeframe=body.timeframe,
    )


_MANUAL_STRATEGY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def register_manual_backtest_strategy(body: ManualStrategyBody) -> dict:
    """Validate + register a user-authored strategy for the manual backtester.

    Unlike the agent/crucible intake path, this does NOT create a lifecycle
    container — the strategy is only registered in the runtime registry so the
    manual backtester can run it. It never enters the autonomous pipeline.

    Returns ``{valid, registered, strategy_name, default_params, errors,
    warnings}``. The registered ``strategy_name`` (== the module's TYPE_NAME) is
    what the caller passes to POST /api/backtests.
    """
    import os

    code = str(body.code or "")
    errors: list[str] = []
    warnings: list[str] = []

    # Resolve the TYPE_NAME: explicit body value wins, else parse from the code.
    type_name = str(body.type_name or "").strip().lower()
    if not type_name:
        match = re.search(r"""TYPE_NAME\s*=\s*['"]([^'"]+)['"]""", code)
        if match:
            type_name = match.group(1).strip().lower()
    if not type_name:
        return {"valid": False, "registered": False, "strategy_name": None,
                "default_params": {}, "errors": ["Code must export TYPE_NAME = \"your_strategy_name\" (snake_case)."],
                "warnings": []}
    if not _MANUAL_STRATEGY_TYPE_RE.match(type_name):
        return {"valid": False, "registered": False, "strategy_name": None,
                "default_params": {}, "errors": [f"Invalid TYPE_NAME '{type_name}': use 3-64 lowercase letters/digits/underscores, starting with a letter."],
                "warnings": []}

    # SECURITY: this module is imported into the live API process by discover()
    # below, so its top-level code executes with host privileges. Run the
    # static AST guard (forbidden imports, dynamic exec/eval, dunder access,
    # filesystem/network/subprocess) and REJECT before writing or importing.
    try:
        from forven.sandbox.ast_guard import scan_source
        report = scan_source(code)
        if not report.ok:
            findings = [f"line {f.lineno}: {f.message}" for f in report.findings[:10]]
            return {"valid": False, "registered": False, "strategy_name": type_name,
                    "default_params": {},
                    "errors": ["Strategy code rejected by the security scan:"] + findings,
                    "warnings": []}
    except Exception as exc:  # noqa: BLE001 — never import unscanned code if the guard itself fails
        return {"valid": False, "registered": False, "strategy_name": type_name,
                "default_params": {}, "errors": [f"Security scan failed: {exc}"], "warnings": []}

    # Validate via the self-heal lint + sandbox harness (may auto-fix).
    try:
        from forven.selfheal import validate_strategy_code
        result = validate_strategy_code(code)
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "registered": False, "strategy_name": None,
                "default_params": {}, "errors": [f"Validation error: {exc}"], "warnings": []}

    final_code = result.get("code") or code
    if not result.get("valid"):
        for issue in (result.get("lint_issues") or [])[:10]:
            errors.append(str(issue))
        exec_res = result.get("execution_result") or {}
        if exec_res.get("stderr"):
            errors.append(f"Runtime: {str(exec_res['stderr'])[:400]}")
        if not errors:
            errors.append("Strategy code failed validation (lint or sandbox execution).")
        return {"valid": False, "registered": False, "strategy_name": type_name,
                "default_params": {}, "errors": errors, "warnings": warnings}

    # Guard: don't clobber a builtin or another module's type. Re-submitting the
    # SAME manual strategy (our own manual_*.py file) is allowed (iteration).
    custom_dir = os.path.join(os.path.dirname(__file__), "strategies", "custom")
    os.makedirs(custom_dir, exist_ok=True)
    init_path = os.path.join(custom_dir, "__init__.py")
    if not os.path.exists(init_path):
        with open(init_path, "w", encoding="utf-8") as fh:
            fh.write('"""Custom strategies — registered modules."""\n')
    manual_path = os.path.join(custom_dir, f"manual_{type_name}.py")
    try:
        from forven.strategies.registry import _TYPE_MAP, discover, reset
        discover()
        if type_name in _TYPE_MAP:
            existing_module = str(getattr(_TYPE_MAP[type_name], "__module__", ""))
            our_module = f"forven.strategies.custom.manual_{type_name}"
            # Allow re-submitting our OWN manual strategy (iteration). Reject if the
            # name belongs to a builtin or any other module — never let manual
            # authoring shadow/clobber an existing registered type.
            if existing_module != our_module:
                return {"valid": True, "registered": False, "strategy_name": type_name,
                        "default_params": {},
                        "errors": [f"TYPE_NAME '{type_name}' is already registered by another strategy ({existing_module or 'unknown'}). Choose a unique name."],
                        "warnings": warnings}
    except Exception:
        reset = discover = None  # type: ignore

    with open(manual_path, "w", encoding="utf-8") as fh:
        fh.write(final_code)

    default_params: dict = {}
    registered = False
    try:
        from forven.strategies.registry import _TYPE_MAP, discover, reset
        reset()
        discover()
        cls = _TYPE_MAP.get(type_name)
        if cls is None:
            return {"valid": True, "registered": False, "strategy_name": type_name,
                    "default_params": {},
                    "errors": [f"Saved {os.path.basename(manual_path)} but type '{type_name}' is not in the registry. Ensure the module exports TYPE_NAME = '{type_name}' and STRATEGY_CLASS."],
                    "warnings": warnings}
        registered = True
        try:
            instance = cls(type_name, {})
            params = getattr(instance, "default_params", {})
            if isinstance(params, dict):
                default_params = dict(params)
            from forven.strategies.lookahead_probe import detect_execution_crash, probe_lookahead

            # lookahead-probe-vacuous-pass (2026-07-25): `probe_lookahead` returns
            # the SAME rejection reason the old `detect_lookahead` wrapper did
            # (that wrapper is literally `probe_lookahead(x).reason`), so the
            # accept/reject decision here is unchanged. What it adds is
            # `inconclusive` — the probe compared NOTHING because the strategy
            # never fired on the synthetic walk, so "no leak found" was a
            # statement about an empty comparison set. Never a rejection (being
            # quiet on synthetic data is not evidence of a leak), but the author
            # must not read the green result as "causality verified", so it is
            # surfaced as a warning. This call site could not say that at all
            # before.
            lookahead_verdict = probe_lookahead(instance)
            leak_reason = lookahead_verdict.reason
            if lookahead_verdict.inconclusive:
                warnings.append(
                    "Lookahead not verifiable: "
                    f"{lookahead_verdict.inconclusive}. The causality probe had "
                    "nothing to compare — this is NOT a leak finding, but nothing "
                    "was verified either."
                )
            crash_reason = detect_execution_crash(instance)
            if leak_reason or crash_reason:
                registered = False
                reset()
                try:
                    os.remove(manual_path)
                except OSError:
                    pass
                reasons = [reason for reason in (leak_reason, crash_reason) if reason]
                return {
                    "valid": False,
                    "registered": False,
                    "strategy_name": type_name,
                    "default_params": {},
                    "errors": [f"Causality/runtime probe rejected the strategy: {reason}" for reason in reasons],
                    "warnings": warnings,
                }
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Registered, but could not read default_params: {exc}")
    except Exception as exc:  # noqa: BLE001
        return {"valid": True, "registered": False, "strategy_name": type_name,
                "default_params": {}, "errors": [f"Registration failed: {exc}"], "warnings": warnings}

    if result.get("lint_issues"):
        warnings.append(f"Auto-fixed {len(result['lint_issues'])} lint issue(s) before registering.")

    return {"valid": True, "registered": registered, "strategy_name": type_name,
            "default_params": default_params, "errors": errors, "warnings": warnings}


def send_manual_strategy_to_forge(body: SendToForgeBody) -> dict:
    """Promote a user-authored manual-backtest strategy into the Forge (/lab).

    Creates a lifecycle strategy container at the ``quick_screen`` entry stage —
    the same stage custom-strategy intake uses — so the strategy shows up in the
    Forge and enters the pipeline. Works for both authoring modes:
      - code:   type_ = the registered custom TYPE_NAME, params = its params.
      - visual: type_ = 'rule_engine', params = {spec, _asset} (round-trips via
                build_strategy_from_row so the pipeline can re-run it).
    """
    from forven.strategies.registry import _TYPE_MAP, discover
    from forven.db import create_strategy_container

    discover()
    mode = str(body.mode or "").strip().lower()
    asset = _extract_base_asset_symbol(body.symbol) or "BTC"
    timeframe = str(body.timeframe or "1h").strip() or "1h"

    # Set only by the `code` branch, which is the one that runs the causality
    # probe; the visual branch builds a rule_engine spec that has no vectorized
    # path to probe. Surfaced on the response so the Forge can say "sent, but
    # causality was not verifiable" instead of implying a clean probe.
    lookahead_inconclusive: str | None = None

    if mode == "visual":
        spec = body.spec if isinstance(body.spec, dict) else None
        if not spec:
            raise HTTPException(status_code=400, detail="Visual strategy spec is required.")
        try:
            from forven.strategies.builtin.rule_engine import validate_rule_spec
            spec_errors = validate_rule_spec(spec)
        except Exception:
            spec_errors = []
        if spec_errors:
            raise HTTPException(status_code=400, detail="Invalid rule spec: " + "; ".join(spec_errors[:5]))
        strategy_type = "rule_engine"
        params: dict = {"spec": spec, "_asset": asset}
        source_ref = "manual_backtest:visual_builder"
        name = (body.name or "").strip() or f"{asset} rule strategy"
    elif mode == "code":
        type_name = str(body.type_name or "").strip()
        if not type_name or type_name not in _TYPE_MAP:
            raise HTTPException(
                status_code=400,
                detail=f"Strategy type '{type_name}' is not registered — validate & load it first.",
            )
        strategy_type = type_name
        params = dict(body.params) if isinstance(body.params, dict) else {}
        try:
            from forven.strategies.lookahead_probe import detect_execution_crash, probe_lookahead

            probe = _TYPE_MAP[type_name](type_name, params)
            # See register_manual_backtest_strategy: `probe_lookahead(x).reason`
            # IS what `detect_lookahead(x)` returned, so intake rejects exactly
            # the same strategies as before. `inconclusive` is the part this
            # gate could not express — the probe compared nothing, so the pass
            # is not evidence of causality. It never blocks intake; it rides
            # back on the response.
            lookahead_verdict = probe_lookahead(probe)
            leak_reason = lookahead_verdict.reason
            lookahead_inconclusive = lookahead_verdict.inconclusive
            crash_reason = detect_execution_crash(probe)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Strategy validation probe failed: {exc}") from exc
        if leak_reason or crash_reason:
            detail = "; ".join(reason for reason in (leak_reason, crash_reason) if reason)
            raise HTTPException(status_code=400, detail=f"Strategy rejected by Forge intake: {detail}")
        params.setdefault("_asset", asset)
        source_ref = f"manual_backtest:custom/manual_{type_name}.py"
        name = (body.name or "").strip() or f"{asset} {type_name}"
    else:
        raise HTTPException(status_code=400, detail="mode must be 'code' or 'visual'")

    try:
        with get_db() as conn:
            strategy_id, display_id, _ = create_strategy_container(
                conn=conn,
                name=name[:140],
                type_=strategy_type,
                symbol=asset,
                timeframe=timeframe,
                params=params,
                stage="quick_screen",
                source="manual_backtest",
                source_ref=source_ref,
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not create strategy: {exc}") from exc

    try:
        log_activity(
            "info", "strategy_intake",
            f"Manual strategy sent to Forge: {strategy_id} ({strategy_type})",
            {"strategy_id": strategy_id, "type": strategy_type, "source": "manual_backtest", "stage": "quick_screen"},
        )
    except Exception:
        pass

    return {
        "ok": True,
        "strategy_id": strategy_id,
        "display_id": display_id,
        "stage": "quick_screen",
        "type": strategy_type,
        "lookahead_inconclusive": lookahead_inconclusive,
    }


def _collect_backtest_execution_controls(payload: object) -> dict[str, object]:
    fields = (
        "trade_mode",
        "allow_shorting",
        "stop_loss_pct",
        "take_profit_pct",
        "trailing_stop_pct",
        "time_stop_bars",
        "sizing_mode",
        "fixed_size",
        "risk_per_trade",
        "atr_stop_multiplier",
        "kelly_multiplier",
        "kelly_lookback",
    )
    controls: dict[str, object] = {}
    for field_name in fields:
        if isinstance(payload, dict):
            value = payload.get(field_name)
        else:
            value = getattr(payload, field_name, None)
        controls[field_name] = value
    return controls


def _collect_honored_backtest_execution_controls(payload: object) -> dict[str, object]:
    fields = (
        "sizing_mode",
        "fixed_size",
        "risk_per_trade",
        "atr_stop_multiplier",
        "kelly_multiplier",
        "kelly_lookback",
        "stop_loss_pct",
        "take_profit_pct",
        "trailing_stop_pct",
        "time_stop_bars",
    )
    controls: dict[str, object] = {}
    for field_name in fields:
        if isinstance(payload, dict):
            value = payload.get(field_name)
        else:
            value = getattr(payload, field_name, None)
        if value is not None:
            controls[field_name] = value
    return controls


def _execution_profile_parity_warnings(controls: dict | None, leverage: float | None = None) -> list[str]:
    """Warn when a backtest's execution profile cannot be reproduced by the live
    (paper/live) risk path, so its returns won't translate to deployment.

    Near-zero false positives: it evaluates parity ONLY when an execution profile
    is explicitly set, and stays silent when the profile genuinely matches live —
    risk-budget sizing (fraction/atr) within the live per-trade cap, no
    live-unsupported exits, sane leverage. Live limits are resolved live so an
    operator who raised their per-trade cap isn't warned with a stale threshold.
    """
    controls = controls if isinstance(controls, dict) else {}
    # A default backtest carries NO execution profile (empty controls) — that is
    # the known legacy full-notional default, not an operator choice, so warning on
    # it every time (and persisting it to every history row) is pure noise. Only an
    # explicitly-set profile is worth a parity check.
    if not controls:
        return []
    warnings: list[str] = []
    try:
        from forven.exchange.risk import _get_risk_limits

        limits = _get_risk_limits()
        # The live HARD cap is max_risk_per_trade (can_open rejects above it and
        # otherwise honors the requested risk in full); per_strategy_max is only a
        # default, so comparing against it false-positives at the common 1-2% risk.
        per_trade_cap = float(limits.get("max_risk_per_trade", 0.02) or 0.02)
    except Exception:
        per_trade_cap = 0.02

    sizing_mode = str(controls.get("sizing_mode") or "full").strip().lower() or "full"
    if sizing_mode not in ("fraction", "atr"):
        warnings.append(
            f"Backtest sizing '{sizing_mode}' is not used live — the live path sizes by risk budget "
            f"over the stop distance, so these returns may not be achievable live."
        )
    else:
        try:
            effective_risk = float(controls.get("risk_per_trade") or 0.02)
        except (TypeError, ValueError):
            effective_risk = 0.02
        if effective_risk > per_trade_cap + 1e-9:
            warnings.append(
                f"Backtest risk/trade (~{effective_risk:.1%}) exceeds the live per-trade cap ({per_trade_cap:.1%}) — "
                f"live will reject or shrink it, understating drawdown and overstating returns."
            )
    # Trailing stops and time-stops ARE enforced live on the kernel execution
    # path (the default: execution_kernel reads the same profile the backtest
    # does). Only the LEGACY non-kernel scanner path ignores them, so warn
    # conditionally instead of claiming "no live equivalent" (stale pre-kernel
    # text that told operators an enforced control was unenforced).
    try:
        from forven.scanner import _live_kernel_execution_enabled

        _kernel_live = bool(_live_kernel_execution_enabled())
    except Exception:
        _kernel_live = True
    if not _kernel_live:
        if controls.get("trailing_stop_pct") is not None:
            warnings.append("Trailing stop is only enforced live on the kernel execution path, which is disabled (live_kernel_execution=off); the live edge may differ.")
        if controls.get("time_stop_bars") is not None:
            warnings.append("Time-stop (N-bar exit) is only enforced live on the kernel execution path, which is disabled (live_kernel_execution=off); the live edge may differ.")
    try:
        lev = float(leverage) if leverage is not None else None
    except (TypeError, ValueError):
        lev = None
    if lev is not None and lev > 10:
        warnings.append(f"Backtest leverage {lev:g}x is far above what the live risk budget can deploy; high-leverage sim returns won't translate.")
    return warnings


def _validate_local_backtest_risk_controls(
    params: dict | None,
    *,
    extra_controls: dict | None = None,
) -> str | None:
    try:
        from forven.strategies import backtest as backtest_mod
    except ImportError:
        return None
    validator = getattr(backtest_mod, "validate_backtest_risk_controls", None)
    if callable(validator):
        return validator(params, extra_controls=extra_controls)
    return None


def _resolve_local_backtest_execution_params(
    strategy_type: str | None,
    raw_params: dict | None,
    *,
    definition_json: dict | None = None,
    allow_uncertified: bool = False,
) -> tuple[dict, str | None]:
    from forven.strategies.certification import (
        EXECUTION_CERTIFIED_FAMILIES,
        certify_execution_strategy,
    )
    from forven.strategies.params import extract_execution_params_from_rule_blobs

    candidates: list[dict] = []
    if isinstance(raw_params, dict):
        candidates.append(dict(raw_params))
    if isinstance(definition_json, dict):
        definition_params = _parse_strategy_params_blob(definition_json.get("params"))
        if definition_params:
            candidates.append(dict(definition_params))
        if definition_json:
            candidates.append(dict(definition_json))
    if not candidates:
        candidates.append({})

    last_canonical_params: dict = {}
    last_error: str | None = None
    for candidate in candidates:
        certification = certify_execution_strategy(strategy_type, candidate)
        last_canonical_params = dict(certification.canonical_params)
        last_error = certification.format_error(context="backtest")
        if certification.certified:
            return last_canonical_params, None
        if not certification.unsupported_rule_blobs:
            continue

        extracted_params = extract_execution_params_from_rule_blobs(strategy_type, candidate)
        if not extracted_params or extracted_params == candidate:
            continue

        extracted_certification = certify_execution_strategy(strategy_type, extracted_params)
        last_canonical_params = dict(extracted_certification.canonical_params)
        last_error = extracted_certification.format_error(context="backtest")
        if extracted_certification.certified:
            return last_canonical_params, None

    # Allow backtesting of novel/uncertified strategy families — the only gate
    # we relax is the family membership check. Param validation errors and
    # unsupported rule blobs still block.
    if allow_uncertified and last_error:
        normalized = str(strategy_type or "").strip().lower()
        family_unknown = normalized and normalized not in EXECUTION_CERTIFIED_FAMILIES
        cert = certify_execution_strategy(strategy_type, last_canonical_params or raw_params or {})
        only_family_block = (
            family_unknown
            and not cert.unsupported_rule_blobs
            and not cert.param_validation_errors
        )
        if only_family_block:
            return last_canonical_params or dict(raw_params or {}), None

    return last_canonical_params, last_error


def _is_canonical_backtest_submit(
    body: "BacktestSubmitBody",
    *,
    strategy_row: dict,
    base_params: dict,
    merged_params: dict,
    execution_params: dict,
    asset: str,
    timeframe: str,
    manual_execution_controls: dict,
    settings: dict,
) -> bool:
    """Return True only when a submitted backtest is a plain rerun of the
    strategy's own stored configuration (params/symbol/timeframe) over roughly
    the default rolling window ending now.

    Only such canonical runs may refresh stored strategy metrics or trigger
    quick_screen auto-promotion (audit B-6): runs with custom params, manual
    execution controls, overridden costs/trade-mode, or short/historical
    windows produce metrics that do not describe the strategy as stored, and
    the best-of-Sharpe sync rule would stamp them onto the row permanently.
    """
    if manual_execution_controls:
        return False
    if merged_params != base_params:
        return False
    if isinstance(body.definition_json, dict) and body.definition_json != _parse_strategy_params_blob(
        strategy_row.get("definition_json")
    ):
        return False
    if body.fee_bps is not None or body.slippage_bps is not None:
        return False
    if str(body.trade_mode or "").strip() or body.allow_shorting is not None:
        return False
    if body.leverage is not None:
        stored_leverage = _coerce_legacy_metadata_float(execution_params.get("leverage"), None)
        if stored_leverage is None or stored_leverage <= 0:
            stored_leverage = float(settings.get("default_leverage", 1.0) or 1.0)
        if abs(float(body.leverage) - float(stored_leverage)) > 1e-9:
            return False

    stored_symbol = str(strategy_row.get("symbol") or "").strip()
    if stored_symbol and _extract_base_asset_symbol(stored_symbol) != str(asset or "").strip().upper():
        return False
    stored_timeframe = str(strategy_row.get("timeframe") or "").strip().lower()
    if stored_timeframe and stored_timeframe != str(timeframe or "").strip().lower():
        return False

    start_raw = str(body.start or "").strip()
    end_raw = str(body.end or "").strip()
    if not start_raw and not end_raw:
        return True

    # An explicit window still counts as canonical when it matches the
    # configured rolling default (UI forms pre-fill it) — i.e. it ends ~now and
    # spans ~backtest_duration_days. Anything else (short or back-shifted
    # windows) is a custom run.
    try:
        duration_days = float(settings.get("backtest_duration_days", DEFAULT_BACKTEST_DURATION_DAYS) or DEFAULT_BACKTEST_DURATION_DAYS)
    except (TypeError, ValueError):
        duration_days = float(DEFAULT_BACKTEST_DURATION_DAYS)
    now = datetime.now(timezone.utc)

    def _parse_window_ts(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    try:
        end_dt = _parse_window_ts(end_raw) if end_raw else now
        if (now - end_dt) > timedelta(days=3):
            return False
        if start_raw:
            start_dt = _parse_window_ts(start_raw)
            span_days = (end_dt - start_dt).total_seconds() / 86400.0
            if abs(span_days - duration_days) > max(duration_days * 0.10, 3.0):
                return False
    except (TypeError, ValueError):
        # Unparseable window — be safe and skip the metrics sync.
        return False
    return True


def post_backtest_submit(body: BacktestSubmitBody, *, skip_auto_trash: bool = False):
    requested_strategy_id = str(body.strategy_id or body.lifecycle_id or "").strip()
    if not requested_strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required")

    strategy_row = _require_existing_strategy_row(requested_strategy_id)
    strategy_id = str(strategy_row.get("id") or requested_strategy_id).strip()
    strategy_name = str(body.strategy_name or strategy_row.get("name") or strategy_id).strip() or strategy_id
    resolved_symbol = str(strategy_row.get("symbol") or body.symbol or "")
    resolved_timeframe = str(strategy_row.get("timeframe") or body.timeframe or "1h")
    base_params: dict = _parse_strategy_params_blob(strategy_row.get("params"))
    strategy_definition_json = body.definition_json if isinstance(body.definition_json, dict) else _parse_strategy_params_blob(strategy_row.get("definition_json"))
    audit_context: dict | None = None
    inferred_params_for_backfill: dict | None = None
    if not base_params:
        audit_context = _infer_strategy_context_from_task_audit(strategy_id)
        if isinstance(audit_context, dict):
            audit_params = _parse_strategy_params_blob(audit_context.get("params"))
            if audit_params:
                base_params = dict(audit_params)
                inferred_params_for_backfill = dict(audit_params)

    requested_params = body.params if isinstance(body.params, dict) else {}
    merged_params = {**base_params, **requested_params}
    strategy_type = _resolve_backtesting_strategy_type(
        explicit_type=resolve_execution_strategy_type(strategy_row),
        strategy_name=strategy_name or strategy_id,
        params=merged_params,
        payload=strategy_definition_json,
    )
    if not strategy_type and audit_context is None:
        audit_context = _infer_strategy_context_from_task_audit(strategy_id)
    if not strategy_type and isinstance(audit_context, dict):
        strategy_type = _resolve_backtesting_strategy_type(
            explicit_type=audit_context.get("strategy_type"),
            strategy_name=strategy_name or strategy_id,
            params=merged_params,
            payload=strategy_definition_json,
        )
    if not strategy_type:
        detail = f"strategy_type could not be resolved for strategy_id={strategy_id}"
        raw_type = str(strategy_row.get("type") or "").strip().lower()
        if raw_type in {"scan", "manual", "autopilot", "campaign", "code", "core"}:
            detail = (
                f"strategy_type could not be resolved for strategy_id={strategy_id}. "
                f"Stored type '{raw_type}' is a lifecycle source marker, not an executable strategy type."
            )
        raise HTTPException(status_code=400, detail=detail)

    execution_params, execution_param_error = _resolve_local_backtest_execution_params(
        strategy_type,
        merged_params,
        definition_json=strategy_definition_json,
        allow_uncertified=True,
    )
    if execution_param_error:
        raise HTTPException(status_code=400, detail=execution_param_error)

    try:
        _backfill_strategy_type_from_context(
            strategy_id=strategy_id,
            strategy_row=strategy_row,
            inferred_type=strategy_type,
            inferred_params=inferred_params_for_backfill,
        )
    except Exception:
        pass

    leverage_value = _coerce_legacy_metadata_float(body.leverage, None)
    if leverage_value is None:
        leverage_value = _coerce_legacy_metadata_float(execution_params.get("leverage"), None)
    if leverage_value is None or leverage_value <= 0:
        # Operator-configurable default (1x) shared with paper/selection for parity.
        leverage_value = float(get_settings().get("default_leverage", 1.0) or 1.0)
    leverage_value = float(leverage_value)

    settings = get_settings()
    default_backtest_timeframe = str(settings.get("backtest_timeframe") or "1h").strip() or "1h"
    asset = _extract_base_asset_symbol(body.symbol, resolved_symbol)
    timeframe = str(body.timeframe or resolved_timeframe or default_backtest_timeframe or "1h").strip() or "1h"
    bars = _estimate_backtest_bars(body.start, body.end, timeframe, duration_days_override=body.duration_days)
    # Validate only the strategy's own params for genuinely-unenforced risk
    # fields. The body-level execution controls (stops/sizing) are now honoured
    # by the engine via execution_controls, so they must NOT be flagged here —
    # doing so was the audited bug that warned about controls that actually work.
    risk_parity_warning = _validate_local_backtest_risk_controls(execution_params)

    from forven.strategies.backtest import backtest_strategy

    # Manual execution controls — the engine honours these (stops, sizing). Only
    # non-None values are forwarded; an all-None dict normalises back to the
    # legacy full-notional path inside the simulator.
    manual_execution_controls = {
        "sizing_mode": body.sizing_mode,
        "risk_per_trade": body.risk_per_trade,
        "fixed_size": body.fixed_size,
        "atr_stop_multiplier": body.atr_stop_multiplier,
        "kelly_multiplier": body.kelly_multiplier,
        "kelly_lookback": body.kelly_lookback,
        "stop_loss_pct": body.stop_loss_pct,
        "take_profit_pct": body.take_profit_pct,
        "trailing_stop_pct": body.trailing_stop_pct,
        "time_stop_bars": body.time_stop_bars,
    }
    manual_execution_controls = {k: v for k, v in manual_execution_controls.items() if v is not None}

    # B-6: only canonical reruns (the strategy's own params/symbol/timeframe on
    # ~the default window) may refresh stored strategy metrics or auto-promote.
    sync_strategy_state = _is_canonical_backtest_submit(
        body,
        strategy_row=strategy_row,
        base_params=base_params,
        merged_params=merged_params,
        execution_params=execution_params,
        asset=asset,
        timeframe=timeframe,
        manual_execution_controls=manual_execution_controls,
        settings=settings,
    )

    try:
        run = backtest_strategy(
            strategy_id=strategy_id,
            asset=asset,
            strategy_type=strategy_type,
            params=execution_params,
            bars=bars,
            leverage=leverage_value,
            timeframe=timeframe,
            persist_legacy_run=False,
            regime_gate=False,
            sync_strategy_state=sync_strategy_state,
            trade_mode=body.trade_mode,
            allow_shorting=body.allow_shorting,
            start_date=(str(body.start).strip() or None) if body.start else None,
            end_date=(str(body.end).strip() or None) if body.end else None,
            fee_bps=body.fee_bps,
            slippage_bps=body.slippage_bps,
            initial_capital=body.initial_capital,
            execution_controls=manual_execution_controls or None,
            as_of=(str(body.as_of).strip() or None) if body.as_of else None,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not isinstance(run, dict):
        raise HTTPException(status_code=500, detail="invalid backtest payload")
    if run.get("error"):
        raise HTTPException(status_code=400, detail=str(run.get("error")))

    metrics = run.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    oos_metrics = metrics.get("out_of_sample")
    if not isinstance(oos_metrics, dict):
        oos_metrics = {}

    total_return_pct = _coerce_legacy_metadata_float(metrics.get("total_return_pct"), 0.0)
    sharpe = _coerce_legacy_metadata_float(metrics.get("sharpe"), 0.0)
    max_drawdown = _coerce_legacy_metadata_float(metrics.get("max_drawdown_pct"), 0.0)
    total_trades = int(_coerce_legacy_metadata_float(metrics.get("total_trades"), 0.0) or 0)

    evaluation_monthly_return = _coerce_optional_float(oos_metrics.get("monthly_return_pct"))
    evaluation_annualized_return = _coerce_optional_float(oos_metrics.get("annualized_return_pct"))
    evaluation_backtest_months = _coerce_optional_float(oos_metrics.get("backtest_months"))

    # Persist full lookback months for UI consistency with the configured test window.
    full_backtest_months = _coerce_optional_float(metrics.get("lookback_months"))
    if full_backtest_months is None:
        full_backtest_months = _coerce_optional_float(metrics.get("backtest_months"))
    if full_backtest_months is None:
        full_backtest_months = evaluation_backtest_months

    now_iso = _now()
    submit_start = str(body.start or run.get("start_date") or "").strip()
    submit_end = str(body.end or run.get("end_date") or "").strip()
    if not submit_end:
        submit_end = now_iso
    if not submit_start:
        try:
            duration_days = int(body.duration_days) if body.duration_days and int(body.duration_days) > 0 else int(settings.get("backtest_duration_days", DEFAULT_BACKTEST_DURATION_DAYS) or DEFAULT_BACKTEST_DURATION_DAYS)
        except Exception:
            duration_days = DEFAULT_BACKTEST_DURATION_DAYS
        try:
            submit_end_dt = datetime.fromisoformat(submit_end.replace("Z", "+00:00"))
        except Exception:
            submit_end_dt = datetime.now(timezone.utc)
        if submit_end_dt.tzinfo is None:
            submit_end_dt = submit_end_dt.replace(tzinfo=timezone.utc)
        submit_start = (submit_end_dt - timedelta(days=max(duration_days, 1))).isoformat()
    job_id = f"bt_{uuid4().hex[:12]}"
    result_id = f"{strategy_id}-{asset.lower()}-{int(time.time() * 1000)}"

    config_payload: dict[str, object] = {
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "strategy": strategy_id,
        "strategy_type": strategy_type,
        "symbol": asset,
        "timeframe": timeframe,
        "start": submit_start,
        "end": submit_end,
        "params": execution_params,
        "definition_json": strategy_definition_json if isinstance(strategy_definition_json, dict) else None,
        "initial_capital": body.initial_capital,
        "fee_bps": body.fee_bps,
        "slippage_bps": body.slippage_bps,
        "trade_mode": run.get("trade_mode") or body.trade_mode,
        "position_model": run.get("position_model"),
        "allow_shorting": body.allow_shorting,
        "stop_loss_pct": body.stop_loss_pct,
        "take_profit_pct": body.take_profit_pct,
        "trailing_stop_pct": body.trailing_stop_pct,
        "time_stop_bars": body.time_stop_bars,
        "sizing_mode": body.sizing_mode,
        "fixed_size": body.fixed_size,
        "risk_per_trade": body.risk_per_trade,
        "atr_stop_multiplier": body.atr_stop_multiplier,
        "kelly_multiplier": body.kelly_multiplier,
        "kelly_lookback": body.kelly_lookback,
        "leverage": leverage_value,
        "job_id": job_id,
        "preserve_result": bool(body.preserve_result),
        "as_of": (str(body.as_of).strip() or None) if body.as_of else None,
    }
    # Verdict auditability (edge-data-expansion Run 2): stamp the identity of
    # the data this result was scored on so drift is detectable, not remembered.
    try:
        from forven.dataeng.quality_gate import dataset_fingerprint

        config_payload["data_fingerprint"] = dataset_fingerprint(asset, timeframe, as_of=body.as_of)
    except Exception:
        pass
    compact_config = {k: v for k, v in config_payload.items() if v is not None}
    # Flag when this backtest's execution profile can't be reproduced live, so the
    # operator sees it on submit AND on every history row (persisted in config).
    execution_profile_warnings = _execution_profile_parity_warnings(manual_execution_controls, leverage=body.leverage)
    if execution_profile_warnings:
        _existing = compact_config.get("warnings")
        compact_config["warnings"] = (list(_existing) if isinstance(_existing, list) else []) + execution_profile_warnings

    # (A `lifecycle_tag` local lived here. Its only reader was the ChromaDB
    # `store_backtest_result(lifecycle_strategy_id=...)` call removed in
    # 97ac259b; `body.lifecycle_id` is still honoured at the top of this
    # function, where it resolves the strategy row.)

    metrics_for_storage = dict(metrics)
    if full_backtest_months is not None:
        metrics_for_storage["backtest_months"] = float(full_backtest_months)
    if evaluation_monthly_return is not None:
        metrics_for_storage["evaluation_monthly_return_pct"] = float(evaluation_monthly_return)
    if evaluation_annualized_return is not None:
        metrics_for_storage["evaluation_annualized_return_pct"] = float(evaluation_annualized_return)
    if evaluation_backtest_months is not None:
        metrics_for_storage["evaluation_backtest_months"] = float(evaluation_backtest_months)

    # Prefer actual data dates over config (form submission) dates.  The
    # backtest engine sets start_date/end_date in the result to the actual
    # candle window used, which may be narrower than the requested range
    # (e.g. due to bar caps or IS/OOS split).
    actual_start = str(run.get("start_date") or compact_config.get("start") or "").strip()
    actual_end = str(run.get("end_date") or compact_config.get("end") or "").strip()

    _persist_backtest_result_row(
        result_id=result_id,
        strategy_id=strategy_id,
        result_type="backtest",
        symbol=asset,
        timeframe=timeframe,
        start_date=actual_start,
        end_date=actual_end,
        metrics=metrics_for_storage,
        config=compact_config,
        created_at=now_iso,
    )

    if compact_config.get("dropzone_session_id"):
        from forven.ai_dropzone_sessions import touch_session

        touch_session(compact_config["dropzone_session_id"])

    try:
        from forven.quant_skills_extractor import record_backtest_for_learning

        record_backtest_for_learning(
            strategy_id=strategy_id,
            asset=asset,
            strategy_type=str(strategy_type),
            params=merged_params,
            metrics=metrics_for_storage,
            fitness=float(sharpe),
            strategy_name=strategy_name,
            config=compact_config,
        )
    except Exception:
        pass  # learning loop is best-effort; SQLite row already persisted
    _write_backtest_result_artifacts(
        result_id, job_id, run.get("trades"),
        equity_curve=run.get("equity_curve"),
        benchmark_curve=run.get("benchmark_curve"),
        equity_curve_full=run.get("equity_curve_full"),
        benchmark_curve_full=run.get("benchmark_curve_full"),
    )
    try:
        chart_context = _build_backtest_chart_context_payload(
            result_id=result_id,
            asset=asset,
            timeframe=timeframe,
            start_date=actual_start,
            end_date=actual_end,
            strategy_name=strategy_name,
            strategy_type=strategy_type,
            strategy_params=merged_params,
            trades=run.get("trades"),
            warnings=run.get("warnings") if isinstance(run.get("warnings"), list) else None,
        )
        if chart_context is not None:
            _write_backtest_chart_artifacts(result_id, job_id, chart_context)
    except Exception:
        pass

    # Auto-assign best symbol to strategy after persisting backtest result
    try:
        auto_assign_best_symbol(strategy_id)
    except Exception:
        pass  # best-effort; don't break backtest flow

    if not skip_auto_trash and not bool(body.preserve_result):
        auto_trash, auto_reason = _should_auto_trash_backtest_result(
            total_return_pct=float(total_return_pct),
            sharpe=float(sharpe),
            max_drawdown_ratio=float(max_drawdown),
            total_trades=int(total_trades),
        )
        if auto_trash:
            with get_db() as conn:
                _set_backtest_result_trash(conn, result_id, deleted=True)
            log_activity(
                "warning",
                "simulation",
                f"Backtest auto-trashed for {strategy_id} ({asset} {timeframe})",
                {
                    "job_id": job_id,
                    "result_id": result_id,
                    "reason": auto_reason,
                    "total_return_pct": float(_to_percent_points(total_return_pct, 0.0)),
                    "sharpe": float(sharpe),
                    "max_drawdown_ratio": float(max_drawdown),
                    "total_trades": int(total_trades),
                },
            )

    log_activity(
        "info",
        "simulation",
        f"Backtest submitted for {strategy_id} ({asset} {timeframe})",
        {"job_id": job_id, "result_id": result_id, "bars": bars},
    )
    response: dict = {"job_id": job_id, "status": "succeeded", "result_id": result_id}
    if risk_parity_warning:
        response["warning"] = risk_parity_warning
    if execution_profile_warnings:
        response["execution_profile_warning"] = execution_profile_warnings
    return response


def post_optimization_submit(body: OptimizationSubmitBody):
    """Run optimization (grid search + WFA) on a strategy and store result."""
    requested_strategy_id = str(body.strategy_id or body.lifecycle_id or "").strip()
    if not requested_strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id is required")

    strategy_row = _require_existing_strategy_row(requested_strategy_id)
    strategy_id = str(strategy_row.get("id") or requested_strategy_id).strip()
    strategy_name = str(body.strategy_name or strategy_row.get("name") or strategy_id).strip() or strategy_id
    resolved_symbol = str(strategy_row.get("symbol") or body.symbol or "")
    resolved_timeframe = str(strategy_row.get("timeframe") or body.timeframe or "1h")
    base_params: dict = _parse_strategy_params_blob(strategy_row.get("params"))
    audit_context: dict | None = None
    inferred_params_for_backfill: dict | None = None
    if not base_params:
        audit_context = _infer_strategy_context_from_task_audit(strategy_id)
        if isinstance(audit_context, dict):
            audit_params = _parse_strategy_params_blob(audit_context.get("params"))
            if audit_params:
                base_params = dict(audit_params)
                inferred_params_for_backfill = dict(audit_params)
    strategy_type = _resolve_backtesting_strategy_type(
        explicit_type=resolve_execution_strategy_type(strategy_row),
        strategy_name=strategy_name or strategy_id,
        params=base_params,
        payload=body.definition_json,
    )
    if not strategy_type and audit_context is None:
        audit_context = _infer_strategy_context_from_task_audit(strategy_id)
    if not strategy_type and isinstance(audit_context, dict):
        strategy_type = _resolve_backtesting_strategy_type(
            explicit_type=audit_context.get("strategy_type"),
            strategy_name=strategy_name or strategy_id,
            params=base_params,
            payload=body.definition_json,
        )
    if not strategy_type:
        detail = f"strategy_type could not be resolved for strategy_id={strategy_id}"
        raw_type = str(strategy_row.get("type") or "").strip().lower()
        if raw_type in {"scan", "manual", "autopilot", "campaign", "code", "core"}:
            detail = (
                f"strategy_type could not be resolved for strategy_id={strategy_id}. "
                f"Stored type '{raw_type}' is a lifecycle source marker, not an executable strategy type."
            )
        raise HTTPException(status_code=400, detail=detail)

    try:
        _backfill_strategy_type_from_context(
            strategy_id=strategy_id,
            strategy_row=strategy_row,
            inferred_type=strategy_type,
            inferred_params=inferred_params_for_backfill,
        )
    except Exception:
        pass

    asset = _extract_base_asset_symbol(body.symbol, resolved_symbol)
    timeframe = str(body.timeframe or resolved_timeframe or "1h").strip() or "1h"
    bars = _estimate_backtest_bars(body.start, body.end, timeframe, duration_days_override=body.duration_days)

    # Generate IDs up front so we can return immediately.
    job_id = f"opt_{uuid4().hex[:12]}"
    result_id = f"opt-{strategy_id}-{asset.lower()}-{int(time.time() * 1000)}"
    now_iso = _now()

    # Compute date range for the placeholder row.
    opt_start_placeholder = str(body.start or "").strip()
    opt_end_placeholder = str(body.end or "").strip() or now_iso
    if not opt_start_placeholder:
        try:
            duration_days = int(body.duration_days) if body.duration_days and int(body.duration_days) > 0 else int(get_settings().get("backtest_duration_days", DEFAULT_BACKTEST_DURATION_DAYS) or DEFAULT_BACKTEST_DURATION_DAYS)
        except Exception:
            duration_days = DEFAULT_BACKTEST_DURATION_DAYS
        try:
            end_dt = datetime.fromisoformat(opt_end_placeholder.replace("Z", "+00:00"))
        except Exception:
            end_dt = datetime.now(timezone.utc)
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        opt_start_placeholder = (end_dt - timedelta(days=max(duration_days, 1))).isoformat()

    body_execution_profile = dict(body.execution_profile) if isinstance(body.execution_profile, dict) else {}
    body_execution_profile.update(_collect_honored_backtest_execution_controls(body))
    body_execution_profile = {k: v for k, v in body_execution_profile.items() if v is not None}
    execution_parameter_ranges = body.execution_parameter_ranges if isinstance(body.execution_parameter_ranges, dict) else None

    # Persist a placeholder row so the UI can see a "running" optimization.
    placeholder_config: dict[str, object] = {
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "strategy": strategy_id,
        "symbol": asset,
        "timeframe": timeframe,
        "start": opt_start_placeholder,
        "end": opt_end_placeholder,
        "base_params": base_params,
        "objective": body.objective,
        "n_trials": body.n_trials,
        "initial_capital": body.initial_capital,
        "fee_bps": body.fee_bps,
        "slippage_bps": body.slippage_bps,
        "leverage": body.leverage,
        "execution_profile": body_execution_profile or None,
        "execution_parameter_ranges": execution_parameter_ranges,
        "parameter_ranges": body.parameter_ranges if isinstance(body.parameter_ranges, dict) else None,
        "job_id": job_id,
        "status": "running",
    }
    compact_placeholder = {k: v for k, v in placeholder_config.items() if v is not None}

    def _format_optimization_error(
        error_like: object,
        *,
        default_message: str = "Optimization failed without an error message",
    ) -> str:
        if isinstance(error_like, BaseException):
            detail = str(error_like).strip()
            if detail:
                return detail
            exc_name = type(error_like).__name__.strip()
            if exc_name == "TimeoutError":
                return "Optimization timed out before a valid result was produced"
            if exc_name and exc_name != "Exception":
                return f"{exc_name}: {default_message}"
            return default_message
        detail = str(error_like or "").strip()
        return detail or default_message

    def _build_failed_optimization_payload(error_detail: str) -> tuple[dict[str, object], dict[str, object]]:
        failed_metrics: dict[str, object] = {
            "status": "failed",
            "error": error_detail,
        }
        if body.n_trials is not None:
            failed_metrics["n_trials"] = int(body.n_trials)
        failed_config = dict(compact_placeholder)
        failed_config["status"] = "failed"
        failed_config["error"] = error_detail
        failed_config["job_id"] = job_id
        return failed_metrics, failed_config

    _persist_backtest_result_row(
        result_id=result_id,
        strategy_id=strategy_id,
        result_type="optimization",
        symbol=asset,
        timeframe=timeframe,
        start_date=opt_start_placeholder,
        end_date=opt_end_placeholder,
        metrics={"status": "running"},
        config=compact_placeholder,
        created_at=now_iso,
    )

    log_activity(
        "info",
        "simulation",
        f"Optimization started for {strategy_id} ({asset} {timeframe})",
        {"job_id": job_id, "result_id": result_id},
    )

    # Capture values the background thread needs.
    # (`opt_lifecycle_tag` and `definition_json` were captured here too. Both fed
    # only the ChromaDB `store_backtest_result(...)` call removed in 97ac259b —
    # the background thread never read either one otherwise, and
    # `body.definition_json` is still used above for strategy resolution.)
    param_space = body.parameter_ranges if isinstance(body.parameter_ranges, dict) else None
    body_objective = body.objective
    body_start = body.start
    body_end = body.end
    body_as_of = body.as_of

    def _run_optimization_background() -> None:
        try:
            from forven.strategies.optimizer import optimize_strategy

            opt_result = optimize_strategy(
                strategy_id=strategy_id,
                asset=asset,
                strategy_type=strategy_type,
                bars=bars,
                param_space=param_space,
                base_params=base_params,
                timeframe=timeframe,
                objective=body_objective,
                n_trials=body.n_trials,
                start_date=body_start,
                end_date=body_end,
                execution_profile=body_execution_profile or None,
                execution_param_space=execution_parameter_ranges,
                fee_bps=body.fee_bps,
                slippage_bps=body.slippage_bps,
                initial_capital=body.initial_capital,
                leverage=body.leverage,
                as_of=body_as_of,
            )

            if not isinstance(opt_result, dict) or opt_result.get("error"):
                error_detail = _format_optimization_error(
                    opt_result.get("error") if isinstance(opt_result, dict) else None,
                    default_message="invalid optimization payload",
                )
                failed_metrics, failed_config = _build_failed_optimization_payload(error_detail)
                _update_optimization_result_row(
                    result_id=result_id,
                    metrics=failed_metrics,
                    config=failed_config,
                )
                log_activity("error", "simulation", f"Optimization failed for {strategy_id}: {error_detail}", {"job_id": job_id})
                return

            best_params = opt_result.get("best_params", {})
            best_full_params = opt_result.get("best_full_params", {})
            best_execution_controls = opt_result.get("best_execution_controls", {})
            best_execution_profile = opt_result.get("best_execution_profile", {})
            best_metrics = opt_result.get("best_metrics", {})
            best_fitness = _coerce_legacy_metadata_float(opt_result.get("best_fitness"), 0.0)
            best_objective_value = _coerce_legacy_metadata_float(opt_result.get("best_objective_value"), best_fitness)

            optimization_start = str(body_start or best_metrics.get("start_date") or best_metrics.get("start") or opt_start_placeholder).strip()
            optimization_end = str(body_end or best_metrics.get("end_date") or best_metrics.get("end") or opt_end_placeholder).strip()

            config_payload: dict[str, object] = {
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "strategy": strategy_id,
                "symbol": asset,
                "timeframe": timeframe,
                "start": optimization_start,
                "end": optimization_end,
                "params": best_params,
                "best_params": best_params,
                "best_full_params": best_full_params if isinstance(best_full_params, dict) else None,
                "base_params": base_params,
                "objective": body_objective,
                "n_trials": body.n_trials,
                "initial_capital": body.initial_capital,
                "fee_bps": placeholder_config.get("fee_bps"),
                "slippage_bps": placeholder_config.get("slippage_bps"),
                "leverage": body.leverage,
                "base_execution_profile": body_execution_profile or None,
                "best_execution_controls": best_execution_controls if isinstance(best_execution_controls, dict) else None,
                "execution_profile": best_execution_profile if isinstance(best_execution_profile, dict) else None,
                "execution_parameter_ranges": execution_parameter_ranges,
                "parameter_ranges": param_space,
                "best_fitness": best_fitness,
                "best_objective_value": best_objective_value,
                "wfa_verdict": opt_result.get("wfa_verdict"),
                "validated": opt_result.get("validated"),
                "holdout_applied": opt_result.get("holdout_applied"),
                "selection_window": opt_result.get("selection_window"),
                "validation_window": opt_result.get("validation_window"),
                "as_of": body_as_of,
                "top_results": opt_result.get("top_results"),
                "job_id": job_id,
                "status": "succeeded",
            }
            compact_config = {k: v for k, v in config_payload.items() if v is not None}

            metrics_for_storage = dict(best_metrics) if isinstance(best_metrics, dict) else {}
            metrics_for_storage["best_fitness"] = float(best_fitness)
            metrics_for_storage["best_objective_value"] = float(best_objective_value)
            metrics_for_storage["status"] = "succeeded"
            if isinstance(best_params, dict):
                metrics_for_storage["best_params"] = best_params
            # Persist the ACTUAL selection breadth (combos evaluated, stamped by the
            # optimizer) — the caller's requested budget is only a fallback. This is
            # what the Deflated Sharpe reads as n_trials; storing the request
            # understates the deflation when the grid ran wider than asked.
            try:
                actual_trials = int(opt_result.get("n_trials") or 0)
            except (TypeError, ValueError):
                actual_trials = 0
            if actual_trials > 0:
                metrics_for_storage["n_trials"] = actual_trials
            elif body.n_trials is not None:
                metrics_for_storage.setdefault("n_trials", int(body.n_trials))
            # Cross-trial Sharpe dispersion: lets the DSR use the real trial
            # variance instead of its conservative estimator proxy.
            if opt_result.get("trial_sharpe_var") is not None:
                try:
                    metrics_for_storage["trial_sharpe_var"] = float(opt_result["trial_sharpe_var"])
                    metrics_for_storage["trial_sharpe_count"] = int(opt_result.get("trial_sharpe_count") or 0)
                except (TypeError, ValueError):
                    pass
            if body_objective is not None:
                metrics_for_storage.setdefault("objective", body_objective)
            if opt_result.get("wfa_verdict") is not None:
                metrics_for_storage["wfa_verdict"] = opt_result.get("wfa_verdict")
            if opt_result.get("validated") is not None:
                metrics_for_storage["validated"] = bool(opt_result.get("validated"))

            _update_optimization_result_row(
                result_id=result_id,
                metrics=metrics_for_storage,
                config=compact_config,
            )

            try:
                from forven.quant_skills_extractor import record_backtest_for_learning

                record_backtest_for_learning(
                    strategy_id=strategy_id,
                    asset=asset,
                    strategy_type=str(strategy_type),
                    params=best_params if isinstance(best_params, dict) else {},
                    metrics=metrics_for_storage,
                    fitness=float(best_fitness),
                    strategy_name=strategy_name,
                    config=compact_config,
                )
            except Exception:
                pass

            try:
                auto_assign_best_symbol(strategy_id)
            except Exception:
                pass

            log_activity(
                "info",
                "simulation",
                f"Optimization completed for {strategy_id} ({asset} {timeframe}), fitness={best_fitness:.1f}",
                {"job_id": job_id, "result_id": result_id, "best_params": best_params},
            )

        except Exception as exc:
            error_detail = _format_optimization_error(exc)
            try:
                failed_metrics, failed_config = _build_failed_optimization_payload(error_detail)
                _update_optimization_result_row(
                    result_id=result_id,
                    metrics=failed_metrics,
                    config=failed_config,
                )
            except Exception:
                pass
            log_activity("error", "simulation", f"Optimization failed for {strategy_id}: {error_detail}", {"job_id": job_id})

    # User-initiated optimizations always get priority access
    is_user = True  # all HTTP-routed optimizations are user-initiated
    max_workers = _optimization_executor_workers()

    with _opt_lock:
        if not is_user:
            available = max_workers - _OPT_USER_RESERVED_SLOTS - _opt_system_running
            if available <= 0:
                error_detail = "optimization executor busy (user slots reserved)"
                failed_metrics, failed_config = _build_failed_optimization_payload(error_detail)
                _update_optimization_result_row(result_id=result_id, metrics=failed_metrics, config=failed_config)
                raise HTTPException(status_code=503, detail=error_detail)

    def _tracked_optimization():
        global _opt_system_running, _opt_user_running
        with _opt_lock:
            if is_user:
                _opt_user_running += 1
            else:
                _opt_system_running += 1
        try:
            _run_optimization_background()
        finally:
            with _opt_lock:
                if is_user:
                    _opt_user_running = max(0, _opt_user_running - 1)
                else:
                    _opt_system_running = max(0, _opt_system_running - 1)

    try:
        _OPTIMIZATION_EXECUTOR.submit(_tracked_optimization)
    except RuntimeError as exc:
        error_detail = _format_optimization_error(
            exc,
            default_message="optimization executor unavailable",
        )
        failed_metrics, failed_config = _build_failed_optimization_payload(error_detail)
        _update_optimization_result_row(
            result_id=result_id,
            metrics=failed_metrics,
            config=failed_config,
        )
        raise HTTPException(status_code=503, detail="optimization executor unavailable") from exc

    from forven.db import set_user_active
    set_user_active()

    return {"job_id": job_id, "status": "running", "result_id": result_id}


def _normalize_backtest_request_source(body: dict) -> str:
    for key in ("request_source", "source", "origin", "triggered_by"):
        value = str(body.get(key) or "").strip().lower()
        if value:
            return re.sub(r"[^a-z0-9_.:-]+", "_", value).strip("_") or "backtesting_api"
    if str(body.get("session_id") or "").strip():
        return "mcp_server"
    return "backtesting_api"


def _backtest_task_title_prefix(request_source: str) -> str:
    normalized = str(request_source or "").strip().lower()
    if normalized in {"agent_tool", "forven_agent_tool", "strategy_developer_tool"}:
        return "Agent Tool Backtest"
    if "mcp" in normalized:
        return "MCP Tool Backtest"
    if normalized in {"ui", "manual", "operator", "user"}:
        return "Operator Backtest"
    return "API Backtest"


def _operator_backtest_source(request_source: str) -> bool:
    return str(request_source or "").strip().lower() in {"ui", "manual", "operator", "user"}


def _agent_backtest_source(request_source: str) -> bool:
    """True for machine-initiated backtests (autonomous agents, MCP tools).

    These are 'system' provenance — distinct from operator-driven runs and
    from bare/unknown API calls, which fall back to 'manual'.
    """
    normalized = str(request_source or "").strip().lower()
    return (
        normalized in {"agent_tool", "forven_agent_tool", "strategy_developer_tool"}
        or "mcp" in normalized
    )


def _summarize_backtest_result_for_task(result: object) -> dict:
    if not isinstance(result, dict):
        return {}
    metrics = result.get("metrics")
    if not isinstance(metrics, dict):
        metrics = {}
    summary = {
        "ok": not bool(result.get("error")),
        "result_id": result.get("result_id"),
        "job_id": result.get("job_id"),
        "error": result.get("error"),
        "total_trades": metrics.get("total_trades"),
        "sharpe": metrics.get("sharpe"),
        "profit_factor": metrics.get("profit_factor"),
        "max_drawdown_pct": metrics.get("max_drawdown_pct"),
        "total_return_pct": metrics.get("total_return_pct"),
    }
    return {k: v for k, v in summary.items() if v not in (None, "")}


def _create_inline_backtest_task(
    *,
    body: dict,
    strategy_id: str,
    dataset_id: object,
    symbol: str,
    timeframe: str,
    strategy_type: str,
    params: dict,
) -> tuple[int | None, str | None, str]:
    """Create the task row that surfaces synchronous API/tool backtests."""
    from forven.db import get_db as _get_db, next_container_id

    request_source = _normalize_backtest_request_source(body)
    started_at = datetime.now(timezone.utc).isoformat()
    display_id: str | None = None
    task_id: int | None = None
    operator_source = _operator_backtest_source(request_source)
    agent_source = not operator_source and _agent_backtest_source(request_source)
    if operator_source:
        task_assigned_by, task_source = "operator", "user"
    elif agent_source:
        task_assigned_by, task_source = "system", "system"
    else:
        task_assigned_by, task_source = "manual", "manual"
    provenance = {
        "request_source": request_source,
        "endpoint": "/api/backtesting/run",
        "strategy_id": strategy_id,
        "dataset_id": dataset_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "strategy_type": strategy_type,
        "parameters": params if isinstance(params, dict) else {},
        "session_id": str(body.get("session_id") or "").strip() or None,
        "origin_agent_id": str(body.get("origin_agent_id") or "").strip() or None,
        "origin_task_id": str(body.get("origin_task_id") or body.get("origin_task_display_id") or "").strip() or None,
    }
    input_payload = {k: v for k, v in provenance.items() if v not in (None, "", {})}
    audit_log = [
        {
            "event": "created",
            "timestamp": started_at,
            "request_source": request_source,
            "endpoint": "/api/backtesting/run",
        },
        {
            "event": "started",
            "timestamp": started_at,
            "agent_id": "simulation-agent",
        },
    ]
    title = f"{_backtest_task_title_prefix(request_source)}: {strategy_id}"
    description = (
        f"Run {strategy_id} on {dataset_id or symbol} at {timeframe} via "
        f"{request_source}."
    )

    try:
        with _get_db() as conn:
            display_id = next_container_id(conn, "T")
            cursor = conn.execute(
                """
                INSERT INTO agent_tasks (
                    agent_id, type, title, description, input_data, display_id,
                    strategy_id, output_data, audit_log, status, assigned_by,
                    priority, created_at, started_at, source
                )
                VALUES (
                    'simulation-agent', 'backtest', ?, ?, ?, ?, ?, NULL, ?,
                    'running', ?, 0, ?, ?, ?
                )
                """,
                (
                    title,
                    description,
                    json.dumps(input_payload, default=str),
                    display_id,
                    strategy_id,
                    json.dumps(audit_log, default=str),
                    task_assigned_by,
                    started_at,
                    started_at,
                    task_source,
                ),
            )
            task_id = int(cursor.lastrowid) if cursor.lastrowid else None
    except Exception as exc:
        log.warning(
            "agent_tasks insert failed for inline backtest %s: %s; Now Working panel will not surface this run",
            strategy_id,
            exc,
        )
        task_id = None
        display_id = None

    return task_id, display_id, request_source


def _finalize_inline_backtest_task(
    *,
    task_id: int | None,
    status: str,
    result: object = None,
    error: object = None,
) -> None:
    if task_id is None:
        return
    from forven.db import append_task_audit_event, get_db as _get_db

    completed_at = datetime.now(timezone.utc).isoformat()
    output_payload = _summarize_backtest_result_for_task(result)
    error_text = str(error or output_payload.get("error") or "").strip() or None
    try:
        with _get_db() as conn:
            conn.execute(
                """
                UPDATE agent_tasks
                   SET status = ?,
                       completed_at = ?,
                       output_data = ?,
                       error = ?
                 WHERE id = ?
                """,
                (
                    status,
                    completed_at,
                    json.dumps(output_payload, default=str) if output_payload else None,
                    error_text,
                    int(task_id),
                ),
            )
            append_task_audit_event(
                conn,
                int(task_id),
                "completed" if status == "done" else "failed",
                {
                    "status": status,
                    "error": error_text,
                    "summary": output_payload,
                },
            )
    except Exception as exc:
        log.warning("agent_tasks status update failed for task_id=%s: %s", task_id, exc)


def post_backtesting_run(body: dict):
    """Start a new backtesting run or AI-driven discovery session."""
    from forven.db import set_user_active
    set_user_active()
    try:
        # Check if this is a single backtest run (from BacktestingClient)
        if "strategy_id" in body and "dataset_id" in body:
            from forven.backtesting import get_client
            client = get_client()
            
            # Extract symbol and timeframe from dataset_id FIRST (before is_remote check)
            # dataset_id format: "BTC/USDT-4h-ccxt" or "BTC/USDT 1h" (legacy)
            # Also check body.timeframe as override
            dataset_id = str(body.get("dataset_id", ""))
            # Strip Forven dataset prefix (e.g., "dataset-26-" from "dataset-26-BTC/USDT-1h")
            dataset_id = re.sub(r"^dataset-\d+-", "", dataset_id)
            # Priority: body.timeframe > parse from dataset_id > default "1h"
            explicit_timeframe = body.get("timeframe")
            
            # Parse symbol and timeframe from dataset_id - always extract symbol
            VALID_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h", "1d", "1w")
            parts = dataset_id.split("-")
            if len(parts) >= 2 and parts[-2] in VALID_TIMEFRAMES:
                timeframe = parts[-2]
                symbol = "-".join(parts[:-2]) if len(parts) > 2 else parts[0]
            elif len(parts) == 2 and parts[1] in VALID_TIMEFRAMES:
                # Handle 2-part hyphenated format: BTC/USDT-1h
                symbol = parts[0]
                timeframe = parts[1]
            elif " " in dataset_id:
                # Legacy space-separated format: "BTC/USDT 1h"
                symbol = dataset_id.split()[0]
                timeframe = dataset_id.split()[-1]
            else:
                # Plain symbol: "BTC/USDT"
                symbol = dataset_id
                timeframe = "1h"
            
            # Override timeframe if explicitly provided
            if explicit_timeframe:
                timeframe = str(explicit_timeframe).strip() or "1h"
            
            # Check if we are pointing to ourself to avoid recursion
            settings = kv_get("forven:settings", {})
            is_remote = settings.get("remote_engine_enabled", False)
            
            if not is_remote:
                # Fallback to local backtest execution
                from forven.strategies.backtest import backtest_strategy
                
                requested_strategy_id = str(body["strategy_id"]).strip()
                strategy_row = _get_strategy_row_by_id(requested_strategy_id)
                if not strategy_row:
                    return {"ok": False, "error": f"strategy not found: {requested_strategy_id}"}
                strategy_id = str(strategy_row.get("id") or requested_strategy_id).strip() or requested_strategy_id
                base_params = _parse_strategy_params_blob(
                    strategy_row.get("params") if strategy_row else {}
                )
                override_params = body.get("parameters")
                if not isinstance(override_params, dict):
                    override_params = {}
                merged_params = dict(base_params)
                merged_params.update(override_params)
                # Run the backtest at the strategy's OWN declared leverage (captured here,
                # before certification may drop the key), not a fixed 3x assumption. An
                # explicit body leverage still wins; engine falls back to 3.0 if neither set.
                _bt_leverage = _coerce_legacy_metadata_float(body.get("leverage"), None)
                if _bt_leverage is None:
                    _bt_leverage = _coerce_legacy_metadata_float(merged_params.get("leverage"), None)
                strategy_type = _resolve_backtesting_strategy_type(
                    explicit_type=body.get("strategy_type")
                    or (resolve_execution_strategy_type(strategy_row) if strategy_row else None),
                    strategy_name=(strategy_row.get("name") if strategy_row else strategy_id) or strategy_id,
                    params=merged_params,
                    payload={
                        "id": strategy_id,
                        "name": strategy_row.get("name") if strategy_row else "",
                        "type": strategy_row.get("type") if strategy_row else "",
                        "params": merged_params,
                    },
                )
                if not strategy_type:
                    return {
                        "ok": False,
                        "error": (
                            f"Could not resolve strategy type for {strategy_id}. "
                            "Set a valid type (macd, rsi_momentum, bollinger, keltner, ema_cross, stochastic)."
                        ),
                    }
                
                # T01403: Validate merged params against certification before execution
                # This fixes the bug where override_params bypassed certification validation
                certified_params, cert_error = _resolve_local_backtest_execution_params(
                    strategy_type,
                    merged_params,
                    allow_uncertified=True,
                )
                if cert_error:
                    return {"ok": False, "error": f"Parameter certification failed: {cert_error}"}
                # Use certified params (canonicalized) instead of raw merged_params
                merged_params = certified_params
                
                # Strategy-param risk controls are still inert and must be
                # guarded, but body-level execution controls are now honoured
                # below through execution_controls just like POST /api/backtests.
                risk_control_error = _validate_local_backtest_risk_controls(merged_params)
                if risk_control_error:
                    return {"ok": False, "error": risk_control_error}
                manual_execution_controls = _collect_honored_backtest_execution_controls(body)

                # Bracket the synchronous backtest in an agent_tasks row so the
                # Now Working panel surfaces API/tool backtests with provenance.
                _nw_task_id, _nw_display_id, _nw_request_source = _create_inline_backtest_task(
                    body=body,
                    strategy_id=strategy_id,
                    dataset_id=body.get("dataset_id"),
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_type=str(strategy_type),
                    params=merged_params,
                )
                _nw_final_status = "failed"
                _nw_error: Exception | None = None
                result = None
                _bars_override = body.get("bars")
                if _bars_override is None and (body.get("start") or body.get("end")):
                    _bars_override = _estimate_backtest_bars(
                        body.get("start"), body.get("end"), timeframe
                    )
                try:
                    result = backtest_strategy(
                        strategy_id=strategy_id,
                        asset=symbol,
                        strategy_type=strategy_type,
                        params=merged_params,
                        timeframe=timeframe,
                        bars=int(_bars_override) if _bars_override else None,
                        leverage=_bt_leverage,
                        persist_legacy_run=False,
                        trade_mode=body.get("trade_mode"),
                        allow_shorting=body.get("allow_shorting"),
                        fee_bps=body.get("fee_bps"),
                        slippage_bps=body.get("slippage_bps"),
                        initial_capital=body.get("initial_capital"),
                        execution_controls=manual_execution_controls or None,
                        start_date=body.get("start") or body.get("start_date"),
                        end_date=body.get("end") or body.get("end_date"),
                        as_of=body.get("as_of"),
                        sync_strategy_state=False,
                    )
                    if isinstance(result, dict) and not result.get("error"):
                        persisted = _persist_completed_backtest_run(
                            strategy_id=strategy_id,
                            strategy_name=(strategy_row.get("name") if strategy_row else strategy_id) or strategy_id,
                            strategy_type=str(strategy_type),
                            asset=symbol,
                            timeframe=timeframe,
                            params=merged_params,
                            run=result,
                            start=body.get("start"),
                            end=body.get("end"),
                            definition_json=body.get("definition_json"),
                            initial_capital=body.get("initial_capital"),
                            fee_bps=body.get("fee_bps"),
                            slippage_bps=body.get("slippage_bps"),
                            trade_mode=result.get("trade_mode") or body.get("trade_mode"),
                            allow_shorting=body.get("allow_shorting"),
                            stop_loss_pct=body.get("stop_loss_pct"),
                            take_profit_pct=body.get("take_profit_pct"),
                            trailing_stop_pct=body.get("trailing_stop_pct"),
                            time_stop_bars=body.get("time_stop_bars"),
                            sizing_mode=body.get("sizing_mode"),
                            fixed_size=body.get("fixed_size"),
                            risk_per_trade=body.get("risk_per_trade"),
                            atr_stop_multiplier=body.get("atr_stop_multiplier"),
                            kelly_multiplier=body.get("kelly_multiplier"),
                            kelly_lookback=body.get("kelly_lookback"),
                            leverage=_bt_leverage,
                            lifecycle_id=body.get("lifecycle_id"),
                            session_id=body.get("session_id"),
                            as_of=body.get("as_of"),
                        )
                        result.setdefault("job_id", str(persisted.get("job_id") or ""))
                        result.setdefault("result_id", str(persisted.get("result_id") or ""))
                        _nw_final_status = "done"
                    if isinstance(result, dict):
                        if _nw_display_id:
                            result.setdefault("task_display_id", _nw_display_id)
                        if _nw_task_id is not None:
                            result.setdefault("task_id", _nw_task_id)
                        result.setdefault("request_source", _nw_request_source)
                    return json_safe_payload(result)
                except Exception as exc:
                    _nw_error = exc
                    raise
                finally:
                    _finalize_inline_backtest_task(
                        task_id=_nw_task_id,
                        status=_nw_final_status,
                        result=result,
                        error=_nw_error,
                    )

            # Remote engine call
            settings_obj = get_settings()
            requested_strategy_id = str(body["strategy_id"]).strip()
            strategy_row = _get_strategy_row_by_id(requested_strategy_id)
            resolved_strategy_id = str((strategy_row or {}).get("id") or requested_strategy_id).strip() or requested_strategy_id
            return json_safe_payload(client.run_backtest(
                strategy_id=resolved_strategy_id,
                dataset_id=body["dataset_id"],
                timeframe=body.get("timeframe"),
                parameters=body.get("parameters"),
                fee_bps=body.get("fee_bps", settings_obj.get("backtest_fee_bps", 4.5)),
                slippage_bps=body.get("slippage_bps", settings_obj.get("backtest_slippage_bps", 2.0)),
                initial_capital=body.get("initial_capital"),
                leverage=body.get("leverage"),
                execution_controls=_collect_honored_backtest_execution_controls(body),
                objective=body.get("objective", "sharpe_ratio"),
                trade_mode=body.get("trade_mode"),
            ))

        # AI-driven Discovery Run (AI Dropzone)
        objective = body.get("objective", "Discover profitable trading strategies")
        symbol_filter = body.get("symbol_filter")
        timeframe_filter = body.get("timeframe_filter")
        prompt_pack = body.get("prompt_pack", "explore")
        max_iterations = int(body.get("max_iterations", 50))
        ide_name = str(body.get("ide_name") or "").strip()[:80]
        prompt_hash = str(body.get("prompt_hash") or "").strip()[:80]
        template_id = str(body.get("template_id") or "").strip()[:80]
        trace_metadata: dict[str, object] = {}
        if ide_name:
            trace_metadata["ide_name"] = ide_name
        if prompt_hash:
            trace_metadata["prompt_hash"] = prompt_hash
        if template_id:
            trace_metadata["template_id"] = template_id

        settings = kv_get("forven:settings", {})
        is_remote = settings.get("remote_engine_enabled", False)
        
        if is_remote:
            from forven.backtesting import get_client
            client = get_client()
            remote_kwargs: dict[str, object] = {}
            remote_kwargs.update(trace_metadata)
            result = client.start_run(
                objective=objective,
                symbol_filter=symbol_filter,
                timeframe_filter=timeframe_filter,
                prompt_pack=prompt_pack,
                max_iterations=max_iterations,
                **remote_kwargs,
            )
        else:
            # Local trigger: assign a high-priority discovery task to strategy-developer
            from forven.brain import assign_task
            trace_lines = []
            if template_id:
                trace_lines.append(f"Template ID: {template_id}")
            if ide_name:
                trace_lines.append(f"IDE Name: {ide_name}")
            if prompt_hash:
                trace_lines.append(f"Prompt Hash: {prompt_hash}")
            trace_block = ""
            if trace_lines:
                trace_block = "\n".join(trace_lines) + "\n"
            task_id = assign_task(
                agent_id="strategy-developer",
                task_type="research",
                title=f"AI Discovery: {symbol_filter or 'All'}",
                description=(
                    f"AI DROPZONE RUN â€” {objective}\n\n"
                    f"Symbol Filter: {symbol_filter or 'None'}\n"
                    f"Timeframe Filter: {timeframe_filter or 'None'}\n"
                    f"Prompt Pack: {prompt_pack}\n"
                    f"Max Iterations: {max_iterations}\n\n"
                    f"{trace_block}"
                    "Goal: Discover, implement, and backtest profitable strategies. "
                    "Use forven_list_datasets to find data, then forven_create_strategy "
                    "and forven_run_backtest to iterate."
                ),
                priority=10,
                source="user",
            )
            result = {"ok": True, "task_id": task_id, "mode": "local"}
            result.update(trace_metadata)
            
        log_activity("info", "backtesting", f"Started AI dropzone run: {prompt_pack}", {
            "objective": objective,
            "symbol_filter": symbol_filter,
            "timeframe_filter": timeframe_filter,
            "mode": "remote" if is_remote else "local",
            **trace_metadata,
        })
        return json_safe_payload(result)
    except Exception as e:
        log_activity("error", "backtesting", f"Failed to start run: {e}")
        return {"error": str(e), "ok": False}


# â”€â”€ Phase 1B: POST endpoints (interactive controls) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


# â”€â”€ Approvals API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


# â”€â”€ Phase 1C: WebSocket â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

async def websocket_endpoint(ws: WebSocket):
    from forven.api_domains import live_ws

    await live_ws.websocket_endpoint(ws)

