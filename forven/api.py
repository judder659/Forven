# SPDX-FileCopyrightText: 2026 Judder <judder@forven.app>
# SPDX-License-Identifier: AGPL-3.0-or-later

import uvicorn
from contextlib import asynccontextmanager
import asyncio
import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from forven.api_core import ForvenV1CompatMiddleware, _on_startup
from forven.api_security import (
    ApiKeyMiddleware,
    CsrfOriginMiddleware,
    assert_auth_keys_configured,
    assert_safe_bind_host,
    get_allowed_cors_origins,
    is_cross_site_state_change,
)
from forven.async_utils import spawn
from forven.correlation import CorrelationIdMiddleware, RequestIdLogFilter
from forven.rate_limiting import install_rate_limiter
from forven.control_plane import (
    ConfirmBody,
    ExecutionModeBody,
    post_emergency_halt,
    post_execution_mode,
    post_trading_halt_reset,
    post_kill_switch_reset,
    post_kill_switch_toggle,
)
from forven.routers.analytics import router as analytics_router
from forven.routers.approvals import router as approvals_router
from forven.routers.auth import router as auth_router
from forven.routers.agents import router as agents_router
from forven.routers.agent_toolsets import router as agent_toolsets_router
from forven.routers.data import router as data_router
from forven.routers.deepdive import router as deepdive_router
from forven.routers.assistant import router as assistant_router
from forven.routers.jobs import router as jobs_router
from forven.routers.legacy import router as legacy_router
from forven.routers.diagnostics import router as diagnostics_router
from forven.routers.mcp import router as mcp_router
from forven.routers.notifications import router as notifications_router
from forven.routers.ops import router as ops_router
from forven.routers.paper import router as paper_router
from forven.routers.status import router as status_router
from forven.routers.strategies import router as strategies_router
from forven.routers.strategy_library import router as strategy_library_router
from forven.routers.system import router as system_router
from forven.routers.tasks import router as tasks_router
from forven.routers.trading import router as trading_router
from forven.routers.websockets import router as websockets_router
from forven.routers.quant_factory import router as quant_factory_router
from forven.routers.routines import router as routines_router
from forven.routers.profile import router as profile_router
from forven.routers.webhooks import router as webhooks_router
from forven.routers.updates import router as updates_router
from forven.routers.backtesting import router as backtesting_router
from forven.routers.lifecycle import router as lifecycle_router
from forven.routers.verdict import router as verdict_router
from forven.routers.robustness import router as robustness_router
from forven.routers.gauntlet import router as gauntlet_router
from forven.routers.lab_regime import router as lab_regime_router
from forven.routers.bot_factory import router as bot_factory_router
from forven.routers.wallets import router as wallets_router
from forven.routers.brain import router as brain_router
from forven.routers.strategy_guard import router as strategy_guard_router
from forven.routers.skills import router as skills_router
from forven.routers.health import router as health_router
from forven.routers.hypotheses import router as hypotheses_router, data_gap_router
from forven.lab_dormancy import quiesce_regime_lab
from forven.lab_features import regime_lab_enabled
from forven.runtime_worker import (
    acquire_runtime_worker_lock,
    run_headless_brain_loop,
    release_runtime_worker_lock,
    run_headless_agent_loop,
    stop_background_task,
)

log = logging.getLogger("forven.api")


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    try:
        value = int(raw) if raw else int(default)
    except (TypeError, ValueError):
        value = int(default)
    value = max(int(minimum), value)
    if maximum is not None:
        value = min(int(maximum), value)
    return value


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float(name: str, default: float, *, minimum: float = 0.0, maximum: float | None = None) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


async def _supervise_background_loop(
    name: str,
    factory: Callable[[], Awaitable[object]],
    *,
    restart_seconds: float = 5.0,
) -> None:
    """Restart critical API-owned loops if one exits unexpectedly.

    A factory may decline to run for a stable, expected reason (e.g. the daemon
    singleton lock is already held by another instance) by returning a value
    whose ``stop_supervision`` attribute is truthy. That is NOT a crash: the
    supervisor stops instead of hot-restarting, which previously produced an
    endless ~5s respawn + log-spam loop.
    """
    while True:
        try:
            result = await factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s crashed; restarting in %.1fs", name, restart_seconds)
        else:
            if getattr(result, "stop_supervision", False):
                log.info("%s declined to start (%s); not supervising", name, result)
                return
            log.warning("%s exited; restarting in %.1fs", name, restart_seconds)
        await asyncio.sleep(max(1.0, float(restart_seconds)))


def _spawn_supervised_loop(name: str, factory: Callable[[], Awaitable[object]]) -> asyncio.Task:
    return spawn(_supervise_background_loop(name, factory), name=name)


def _spawn_supervised_runtime_thread(
    name: str,
    factory: Callable[[], Awaitable[object]],
    *,
    initial_delay_seconds: float = 0.0,
) -> threading.Thread:
    """Run a long-lived runtime coroutine away from uvicorn's request loop.

    The scheduler, headless workers, and market daemon perform DB, exchange, and
    model-provider work. Even when individual jobs try to offload blocking work,
    one missed synchronous path can starve the event loop. Keeping these loops
    on their own event loops preserves HTTP and websocket liveness.
    """

    def _runner() -> None:
        try:
            if initial_delay_seconds > 0:
                threading.Event().wait(initial_delay_seconds)
            asyncio.run(_supervise_background_loop(name, factory))
        except Exception:
            log.exception("%s runtime thread crashed", name)

    thread = threading.Thread(target=_runner, name=f"forven-{name}", daemon=True)
    thread.start()
    return thread


# Windows Proactor loop can intermittently reset accepted sockets under mixed
# HTTP/WebSocket load. Use selector policy for API stability on Windows.
if sys.platform.startswith("win"):
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    except Exception:
        pass


_RUNTIME_LOGGING_CONFIGURED = False


def _configure_runtime_logging() -> None:
    """Wire up file/stdout logging exactly once, regardless of launcher.

    The scheduler, autonomous loop, and agent workers all log at INFO. Under the
    production launcher (``python -m uvicorn forven.api:app``) the ``__main__``
    block at the bottom of this module never runs, so without this the loop
    produces NO observable log lines and "is it moving?" is unanswerable from
    logs. Guarded so repeated calls (and the ``__main__`` path) never
    double-configure handlers. Uvicorn's named loggers set propagate=False, so
    its access logs are unaffected by reconfiguring the root logger.
    """
    global _RUNTIME_LOGGING_CONFIGURED
    if _RUNTIME_LOGGING_CONFIGURED:
        return
    _RUNTIME_LOGGING_CONFIGURED = True
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    home_env = os.environ.get("FORVEN_HOME")
    try:
        if home_env:
            from pathlib import Path as _Path

            from forven.logging_config import setup_rotating_file_logger

            log_path = _Path(home_env) / "logs" / "api.log"
            setup_rotating_file_logger(
                log_path, level=logging.INFO, fmt=fmt, also_stdout=True
            )
            logging.getLogger("forven.api").info("Runtime logging -> %s", log_path)
        elif not logging.getLogger().handlers:
            logging.basicConfig(level=logging.INFO, format=fmt)
    except Exception as exc:  # never let logging setup block startup
        sys.stderr.write(f"WARNING: runtime logging setup failed: {exc}\n")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Configure logging FIRST so every subsequent startup step, the scheduler
    # loop, and the agent workers emit observable log lines under any launcher.
    _configure_runtime_logging()

    # Surface missing/incompatible DECLARED dependencies at boot, under ANY
    # launcher. The shell launchers preflight before starting, but a direct
    # `uvicorn forven.api:app` / Docker run would otherwise only discover a
    # lazily-imported missing dep (feedparser, trafilatura, yt-dlp, ...) deep
    # into an operation. Best-effort + WARNING only: the app is already
    # importing, so eager deps are fine; this catches the rest.
    try:
        from forven.preflight import check_dependencies

        _dep_problems = check_dependencies()
        if _dep_problems:
            log.warning(
                "Dependency preflight: %d declared dependency(ies) missing/incompatible "
                "(fix with `pip install -e .`): %s",
                len(_dep_problems),
                "; ".join(_dep_problems),
            )
    except Exception:
        log.exception("Dependency preflight check failed (continuing).")

    _bot_monitor_task: asyncio.Task | None = None
    _scheduler_task: asyncio.Task | None = None
    _agent_worker_task: asyncio.Task | None = None
    _brain_worker_task: asyncio.Task | None = None
    _daemon_task: asyncio.Task | None = None
    _runtime_threads: list[threading.Thread] = []
    _api_runtime_lock_held = False

    assert_auth_keys_configured()

    # Packaged installs: seed $FORVEN_HOME/data/ohlcv/ from bundled parquets
    # so BTC/ETH 1h/4h/1d data is available on first launch — otherwise a
    # fresh install has zero market data and every backtest/dashboard query
    # returns empty. No-op for dev runs (FORVEN_HOME unset) and for repeat
    # launches (targets already exist). Runs BEFORE init_db so anything the
    # scheduler bootstrap consults (e.g. dataset catalog) sees the seed.
    try:
        from forven.config import ensure_seed_data_bootstrapped
        seeded = ensure_seed_data_bootstrapped()
        if seeded:
            log.info("Seeded %d OHLCV parquet file(s) on first launch.", seeded)
    except Exception:
        log.exception("OHLCV seed bootstrap failed (continuing without seed).")

    # Initialize database schema FIRST. Every downstream startup step
    # (template seeding, bot recovery, kv writes, agent seeding) reads or
    # writes SQLite, so schema must exist before any of them run.
    try:
        from forven.db import init_db
        init_db()
    except Exception:
        log.exception("Database schema init failed at startup.")

    try:
        from forven.gauntlet.engine import recover_stale_running_steps

        recovered_gauntlet = recover_stale_running_steps(stale_after_minutes=1)
        if recovered_gauntlet.get("blocked_runtime"):
            log.info("Recovered %d stale gauntlet workflow step(s).", recovered_gauntlet["blocked_runtime"])
    except Exception:
        log.exception("Gauntlet workflow recovery failed at startup.")

    # Auto-resume tasks that were interrupted by the last shutdown (Tauri
    # close->reopen). The shutdown hook flags them 'interrupted'; without this
    # they sat until a manual diagnostics call, silently dropping in-flight work
    # across every restart. Only checkpointed/idempotent tasks are re-queued.
    try:
        from forven.lifecycle import resume_interrupted_tasks_on_startup

        resumed = resume_interrupted_tasks_on_startup()
        if resumed:
            log.info("Re-queued %d interrupted task(s) for resume at startup.", resumed)
    except Exception:
        log.exception("Interrupted-task resume failed at startup.")

    # Seed core Forven agents (brain, quant-researcher, simulation-agent,
    # risk-manager, execution-trader, full-stack-engineer,
    # strategy-developer). This used to run only when
    # the Discord bot connected, which left a fresh install with an empty
    # `agents` table and every PATCH /api/agents/*/model returning 404.
    # Running it here makes the Agent Hub usable without Discord.

    # Spend-safety: seed the in-app "connected providers" set from existing
    # credential profiles (so upgrades keep working without re-connecting) and
    # turn ON the fail-closed model-selection gate. After this the bot only
    # calls providers the operator explicitly connected AND models they selected.
    try:
        from forven.model_selection import (
            enable_enforcement,
            migrate_connected_from_profiles,
        )

        migrate_connected_from_profiles()
        enable_enforcement()
        log.info("Model-selection enforcement enabled (fail-closed routing).")
    except Exception:
        log.exception("Failed to initialize model-selection enforcement.")

    # Seed the workspace identity files on the API-owned runtime. The only other
    # caller (the Discord bot's _bootstrap) is skipped in gateway-only mode,
    # which left SOUL.md/AGENTS.md/IDENTITY.md uncreated. Idempotent.
    try:
        from forven.workspace import init_workspace

        init_workspace()
    except Exception:
        log.exception("Workspace init failed at startup.")

    try:
        from forven.bot import seed_default_agents
        result = seed_default_agents()
        if result.get("created"):
            log.info("Seeded %d core agent(s) at startup: %s", len(result["created"]), result["created"])
    except Exception:
        log.exception("Core agent seeding failed at startup.")

    # Clear any stale simulation locks that might have persisted from a crash
    try:
        from forven.db import kv_set
        kv_set("forven:simulation:active", False)
        kv_set("simulation_state", {"active": False, "phase": "idle"})
        log.info("Cleared stale simulation locks on startup.")
    except Exception:
        pass

    if not regime_lab_enabled():
        try:
            summary = quiesce_regime_lab()
            log.info("Regime Lab quiesced for dormancy: %s", summary)
        except Exception:
            log.exception("Failed to quiesce dormant Regime Lab during startup.")

    # Seed built-in bot templates
    try:
        from forven.bot_factory.templates import seed_builtin_templates
        seeded = seed_builtin_templates()
        if seeded:
            log.info("Seeded %d built-in bot templates.", seeded)
    except Exception:
        log.exception("Failed to seed bot templates.")

    # Recover bots that were running before a crash/restart
    try:
        from forven.bot_factory.manager import BotManager
        result = BotManager.get_instance().recover_bots()
        if result.get("recovered"):
            log.info("Recovered %d bot(s) after restart.", result["recovered"])
    except Exception:
        log.exception("Failed to recover bots on startup.")

    # Phase 4: register MCP server tools (each server's tools become
    # available to granted agents via tool_registry). Per-server failures
    # are logged and suppressed inside register_all_enabled_servers so a
    # bad config never blocks startup.
    try:
        from forven.agents.mcp_client import register_all_enabled_servers
        mcp_results = await register_all_enabled_servers()
        if mcp_results:
            total = sum(mcp_results.values())
            log.info("Registered %d MCP tool(s) from %d server(s).", total, len(mcp_results))
    except Exception:
        log.exception("MCP server tool registration failed (continuing without MCP).")

    await _on_startup()

    # Event-loop lag watchdog: measures request-loop scheduling drift so GIL/loop
    # stalls (the WS-drop mechanism) are logged and surfaced in /api/health instead
    # of showing up only as mysterious client disconnects. Must run on THIS loop.
    try:
        from forven.loop_watchdog import run_loop_watchdog
        _loop_watchdog_task = spawn(run_loop_watchdog(), name="loop-lag-watchdog")
    except Exception:
        log.exception("Failed to start event-loop lag watchdog.")

    # Start bot heartbeat monitor as background task
    try:
        from forven.bot_factory.manager import BotManager
        _bot_monitor_task = spawn(BotManager.get_instance().monitor_bots(), name="bot-monitor")
    except Exception:
        log.exception("Failed to start bot monitor.")

    # Run critical background loops from the API process as a fallback when the
    # Discord bot runtime is unavailable. A file lock ensures only one API
    # process owns these loops.
    try:
        if acquire_runtime_worker_lock():
            _api_runtime_lock_held = True
            from forven.scheduler import reset_scheduler_job_locks, run_scheduler_loop

            recovered_job_locks = reset_scheduler_job_locks()
            if recovered_job_locks:
                log.info("API runtime worker cleared %d inherited scheduler job lock(s) at startup", recovered_job_locks)

            agent_concurrency = _env_int("FORVEN_HEADLESS_AGENT_CONCURRENCY", 3, minimum=1, maximum=8)
            brain_limit = _env_int("FORVEN_HEADLESS_BRAIN_LIMIT", 2, minimum=1, maximum=8)
            runtime_thread_mode = _env_bool("FORVEN_API_RUNTIME_THREAD_MODE", True)
            runtime_start_delay = _env_float("FORVEN_API_RUNTIME_START_DELAY_SECONDS", 5.0, minimum=0.0, maximum=60.0)
            # BOOT-1: close the startup-recovery gate BEFORE the scheduler/scanner thread
            # can run a live execution scan, so a stale recovery_active=False persisted by
            # a cleanly-exited prior run can't let a live entry through during the boot
            # window before the daemon's boot reconcile re-verifies the exchange.
            try:
                from forven.daemon import mark_boot_recovery_pending
                mark_boot_recovery_pending()
            except Exception:
                log.warning("Could not stamp boot recovery gate", exc_info=True)
            if runtime_thread_mode:
                _runtime_threads.extend(
                    [
                        _spawn_supervised_runtime_thread(
                            "scheduler-loop",
                            lambda: run_scheduler_loop(interval_seconds=30),
                            initial_delay_seconds=runtime_start_delay,
                        ),
                        _spawn_supervised_runtime_thread(
                            "headless-agent-loop",
                            lambda: run_headless_agent_loop(poll_seconds=5.0, concurrency=agent_concurrency),
                            initial_delay_seconds=runtime_start_delay,
                        ),
                        _spawn_supervised_runtime_thread(
                            "headless-brain-loop",
                            lambda: run_headless_brain_loop(poll_seconds=20.0, limit=brain_limit),
                            initial_delay_seconds=runtime_start_delay,
                        ),
                    ]
                )
            else:
                _scheduler_task = _spawn_supervised_loop(
                    "scheduler-loop",
                    lambda: run_scheduler_loop(interval_seconds=30),
                )
                _agent_worker_task = _spawn_supervised_loop(
                    "headless-agent-loop",
                    lambda: run_headless_agent_loop(poll_seconds=5.0, concurrency=agent_concurrency),
                )
                _brain_worker_task = _spawn_supervised_loop(
                    "headless-brain-loop",
                    lambda: run_headless_brain_loop(poll_seconds=20.0, limit=brain_limit),
                )

            # Host the data/risk daemon in-process so a Discord-less install
            # (Tauri shell only spawns `python -m forven.api`) still drives the
            # market feed, price ticks, and daemon_state heartbeat. The daemon
            # holds its own file lock so an external `forven daemon start` wins
            # if it's already running.
            try:
                from forven.daemon import run_in_loop as run_daemon_in_loop
                if runtime_thread_mode:
                    _runtime_threads.append(
                        _spawn_supervised_runtime_thread(
                            "daemon-loop",
                            run_daemon_in_loop,
                            initial_delay_seconds=runtime_start_delay,
                        )
                    )
                else:
                    _daemon_task = spawn(run_daemon_in_loop(), name="daemon-loop")
                log.info(
                    "API runtime worker started: scheduler + headless agent loop "
                    "(concurrency=%d) + headless brain loop (limit=%d) + data/risk daemon "
                    "(thread_mode=%s, start_delay=%.1fs)",
                    agent_concurrency,
                    brain_limit,
                    runtime_thread_mode,
                    runtime_start_delay,
                )
            except Exception:
                log.exception("Failed to start in-process daemon loop.")
                # BOOT-1: no daemon will run its boot reconcile in this process, so
                # release the startup-recovery gate we stamped above (else paper trading
                # is wedged forever). Live still fails closed on missing real equity.
                try:
                    from forven.daemon import clear_boot_recovery_pending
                    clear_boot_recovery_pending("daemon_unavailable")
                except Exception:
                    log.warning("Could not release boot recovery gate after daemon-start failure", exc_info=True)
                log.info(
                    "API runtime worker started: scheduler + headless agent loop "
                    "(concurrency=%d) + headless brain loop (limit=%d) (daemon unavailable, thread_mode=%s, start_delay=%.1fs)",
                    agent_concurrency,
                    brain_limit,
                    runtime_thread_mode,
                    runtime_start_delay,
                )
        else:
            log.info("API runtime worker lock not acquired; another process owns background loops")
    except Exception:
        log.exception("Failed to start API runtime worker.")

    # Start unified health monitor
    _health_monitor = None
    try:
        from forven.health_monitor import HealthMonitor, HealthState, set_health_monitor
        _health_state = HealthState()
        _health_monitor = HealthMonitor(state=_health_state)
        set_health_monitor(_health_monitor)
        await _health_monitor.start()
    except Exception:
        # H-R5: persist a flag so /api/health can surface this. Previously the
        # failure was only logged and callers thought the monitor was up.
        log.exception("Failed to start health monitor.")
        try:
            from forven.db import kv_set
            kv_set("forven:health_monitor:unavailable", True)
        except Exception:
            pass
    else:
        try:
            from forven.db import kv_set
            kv_set("forven:health_monitor:unavailable", False)
        except Exception:
            pass

    yield

    # Signal the in-process daemon loop to exit so _price_consumer /
    # async_market_loop break out of their `while not shutdown.is_set()` waits
    # before we cancel the task. Without this the cancel races with in-flight
    # HyperLiquid awaits and can print noisy CancelledError tracebacks.
    try:
        from forven.daemon import shutdown as _daemon_shutdown_event
        _daemon_shutdown_event.set()
    except Exception:
        pass
    await stop_background_task(_daemon_task)

    await stop_background_task(_brain_worker_task)
    await stop_background_task(_agent_worker_task)
    await stop_background_task(_scheduler_task)
    if _runtime_threads:
        log.info("API runtime thread mode shutdown requested for %d thread(s)", len(_runtime_threads))
    if _api_runtime_lock_held:
        release_runtime_worker_lock()

    # Graceful shutdown: any agent task that was still ``running`` when the
    # process exited gets flagged ``interrupted`` so the next app open can
    # offer to resume it (T08 task_progress). Must run AFTER the workers
    # stop so we don't race a worker that is mid-commit.
    try:
        from forven.lifecycle import mark_in_flight_tasks_interrupted
        mark_in_flight_tasks_interrupted()
    except Exception:
        log.exception("mark_in_flight_tasks_interrupted hook failed")

    # Shutdown: stop health monitor
    if _health_monitor is not None:
        try:
            await _health_monitor.stop()
        except Exception:
            pass

    await stop_background_task(_bot_monitor_task)

    # Shutdown: stop all running bots
    try:
        from forven.bot_factory.manager import BotManager
        BotManager.get_instance().shutdown_all()
    except Exception:
        pass


app = FastAPI(
    title="Forven API",
    description="Hub-and-spoke visualization API for the frontend.",
    lifespan=lifespan,
)

app.add_middleware(ForvenV1CompatMiddleware)
app.add_middleware(ApiKeyMiddleware)
# Drive-by CSRF guard: reject cross-site state-changing requests. The local API
# is reachable from a browser at 127.0.0.1, so a page the operator visits could
# POST to it; CORS only hides the response, it does not stop the side effect.
# Same-origin and CORS-allowlisted origins pass; no-Origin (launcher/CLI) passes.
app.add_middleware(CsrfOriginMiddleware)
# DNS rebinding guard. Even though uvicorn is bound to 127.0.0.1, a browser
# page on the tester's machine that resolves attacker-controlled.com to
# 127.0.0.1 can still reach us — the browser just dials the loopback socket
# and sends `Host: attacker-controlled.com`. Rejecting unknown Host values
# closes that door. FORVEN_ALLOWED_HOSTS can extend the list for dev/LAN
# scenarios; packaged builds stick to loopback.
_allowed_hosts_env = os.environ.get("FORVEN_ALLOWED_HOSTS", "").strip()
if _allowed_hosts_env:
    _allowed_hosts = [h.strip() for h in _allowed_hosts_env.split(",") if h.strip()]
else:
    # "testserver" is the hardcoded Host that starlette.testclient.TestClient
    # sends; include it so in-process tests don't 400. It cannot be reached
    # over the network (no public DNS, and uvicorn binds to 127.0.0.1).
    _allowed_hosts = ["127.0.0.1", "localhost", "[::1]", "testserver"]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=_allowed_hosts)
# H-S6: install slowapi rate limiter (no-op if disabled or unavailable).
# Must run before downstream middleware so the SlowAPIMiddleware order is sane.
install_rate_limiter(app)
# CorrelationIdMiddleware runs first so all downstream code (auth, routers,
# log statements) sees the active request_id contextvar.
app.add_middleware(CorrelationIdMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_allowed_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Attach the request_id filter to the root logger so every log line carries
# either the live request_id or "-" (for background tasks). Formatters can
# now reference %(request_id)s without raising KeyError.
_root_logger = logging.getLogger()
if not any(isinstance(f, RequestIdLogFilter) for f in _root_logger.filters):
    _root_logger.addFilter(RequestIdLogFilter())


app.include_router(status_router)
app.include_router(diagnostics_router)
app.include_router(notifications_router)
app.include_router(hypotheses_router)
app.include_router(data_gap_router)
app.include_router(approvals_router)
app.include_router(ops_router)
app.include_router(analytics_router)
app.include_router(data_router)
app.include_router(tasks_router)
app.include_router(trading_router)
app.include_router(paper_router)
app.include_router(jobs_router)
app.include_router(legacy_router)
app.include_router(system_router)
app.include_router(updates_router)
app.include_router(auth_router)
app.include_router(agents_router)
app.include_router(agent_toolsets_router)
app.include_router(routines_router)
app.include_router(profile_router)
app.include_router(strategies_router)
app.include_router(strategy_library_router)
app.include_router(websockets_router)
app.include_router(quant_factory_router)
# Webhooks are a GitHub-driven self-update path that shells out to `git` and
# is auth-exempt by design. That combination has no business existing in a
# packaged tester install — they aren't CI and shouldn't have a `git pull`
# path exposed even over loopback. Only mount it on dev runs.
from forven.config import is_beta_build as _is_beta_build  # noqa: E402
if not _is_beta_build():
    app.include_router(webhooks_router)
app.include_router(backtesting_router)
app.include_router(lifecycle_router)
app.include_router(deepdive_router)
app.include_router(assistant_router)
app.include_router(verdict_router)
app.include_router(robustness_router)
app.include_router(gauntlet_router)
if regime_lab_enabled():
    app.include_router(lab_regime_router)
app.include_router(bot_factory_router)
app.include_router(wallets_router)
app.include_router(brain_router)
app.include_router(strategy_guard_router)
app.include_router(skills_router)
app.include_router(health_router)
app.include_router(mcp_router)


@app.post("/api/shutdown", status_code=202)
async def shutdown(request: Request):
    client_host = request.client.host if request.client else ""
    if client_host not in ("127.0.0.1", "::1"):
        raise HTTPException(status_code=403, detail="localhost only")
    # The loopback check alone is NOT enough: a malicious page the operator visits
    # POSTs here from the browser's own loopback socket, so client_host is 127.0.0.1
    # too. Reject anything carrying a cross-site browser Origin/Referer (the local
    # launcher/controller sends neither). The global CsrfOriginMiddleware also blocks
    # this; this is a local backstop on the highest-value drive-by target.
    if is_cross_site_state_change(request):
        raise HTTPException(status_code=403, detail="cross-origin shutdown rejected")

    async def _exit_soon():
        await asyncio.sleep(0.25)
        # Try graceful: signal uvicorn to stop, which runs lifespan teardown
        # (releases file locks, stops bot subprocesses, flushes WAL). Fall back
        # to hard exit if teardown stalls beyond Tauri's 5s budget.
        try:
            import signal
            sig = signal.SIGBREAK if os.name == "nt" else signal.SIGTERM
            os.kill(os.getpid(), sig)
            await asyncio.sleep(3.0)
        except Exception:
            pass
        os._exit(0)

    asyncio.create_task(_exit_soon())
    return {"status": "shutting_down"}


# Serve the prebuilt SvelteKit bundle at "/" when FORVEN_FRONTEND_DIR points to
# a real directory. Mount is registered last so explicit API routes always win.
# The status_router's bare "GET /" would otherwise shadow the SPA index, so we
# remove it from app.routes before adding the mount.
from fastapi.staticfiles import StaticFiles  # noqa: E402

_frontend_dir = os.environ.get("FORVEN_FRONTEND_DIR")
if _frontend_dir and os.path.isdir(_frontend_dir):
    app.router.routes = [
        r for r in app.router.routes
        if not (getattr(r, "path", None) == "/" and "GET" in (getattr(r, "methods", None) or set()))
    ]
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")


# ── Startup guard: fail fast on duplicate (method, path) route registrations ──
# Two routers registering the same method+path silently shadow each other based
# on include order — exactly how a dead duplicate POST /api/backtesting/run lurked
# undetected. Surface it loudly at construction time rather than shipping a route
# whose behavior depends on import order.
def _assert_no_duplicate_routes(app_) -> None:
    seen: dict[tuple[str, str], int] = {}
    dupes: set[str] = set()
    for route in app_.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            key = (method, path)
            seen[key] = seen.get(key, 0) + 1
            if seen[key] > 1:
                dupes.add(f"{method} {path}")
    if dupes:
        raise RuntimeError(
            "Duplicate route registration(s) detected — two handlers share a "
            "method+path and silently shadow by include order: "
            + ", ".join(sorted(dupes))
        )


_assert_no_duplicate_routes(app)


__all__ = [
    "app",
    "ConfirmBody",
    "ExecutionModeBody",
    "post_execution_mode",
    "post_trading_halt_reset",
    "post_kill_switch_reset",
    "post_kill_switch_toggle",
    "post_emergency_halt",
]


if __name__ == "__main__":
    import argparse
    import socket as _socket

    # Packaged installs (Tauri sets FORVEN_HOME) have no attached console — the
    # backend is spawned with CREATE_NO_WINDOW, so stdout/stderr go nowhere by
    # default. Wire up rotating file logging so agent crashes, scheduler
    # bootstrap failures, and uvicorn tracebacks land somewhere operators can
    # actually read. Dev runs (no FORVEN_HOME) keep the existing stdout-only
    # behavior so `forven api` in a terminal stays chatty.
    # Same logging bootstrap the lifespan uses; idempotent via the module guard.
    # Running it here too means a direct `python -m forven.api` launch still
    # configures logging before uvicorn.run() (so bind/startup errors are
    # captured), while `uvicorn forven.api:app` relies on the lifespan call.
    _configure_runtime_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    port = args.port if args.port is not None else int(os.environ.get("FORVEN_PORT", "8003"))
    # Detect a listener before handing off to uvicorn. On Windows uvicorn's
    # SO_REUSEADDR masks bind-collisions, so we connect-probe instead.
    _probe = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    _probe.settimeout(0.25)
    _in_use = False
    try:
        _probe.connect(("127.0.0.1", port))
        _in_use = True
    except OSError:
        pass
    finally:
        _probe.close()
    if _in_use:
        sys.stderr.write(f"ERROR: port {port} is already in use\n")
        sys.exit(2)
    # Bind to loopback by default so the API isn't reachable from the LAN.
    # FORVEN_BIND_HOST (or the FORVEN_HOST launcher alias) can be set for dev/ops
    # scenarios; if it exposes the API beyond localhost, an API key is required
    # (see assert_safe_bind_host). resolved_bind_host() mirrors launcher precedence
    # so this entry point and the guard never diverge (M6).
    from forven.api_security import resolved_bind_host

    bind_host = resolved_bind_host()
    # Fail closed: never expose an unauthenticated API beyond loopback.
    assert_safe_bind_host(bind_host)
    uvicorn.run(app, host=bind_host, port=port, workers=1)
