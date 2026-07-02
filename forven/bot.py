"""Discord bot — the gateway. Receives messages, calls AI, responds.

This is the equivalent of OpenClaw's gateway. When Judder sends a message:
1. Bot receives it
2. Builds brain context (workspace + SQLite + ChromaDB)
3. Calls AI with the message
4. Posts the response back to the same channel
5. Logs the interaction

Also runs the scheduler loop in-process.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None

import discord
from discord.ext import commands, tasks

from forven.config import FORVEN_HOME, ensure_dirs, load_config
from forven.control_plane.queue_processing import (
    QUEUE_PROCESS_REQUEST_KEY,
    QUEUE_PROCESS_RESULT_KEY,
    QUEUE_PROCESS_STALE_AFTER_SECONDS,
    build_queue_process_result,
    is_active_request,
    parse_timestamp,
    utc_now_iso,
)
from forven.db import create_pending_task, kv_get, kv_set, kv_set_best_effort
from forven.model_routing import get_default_model_for_provider
from forven.notification_policy import DEFAULT_RESPONSE_CHANNEL_ALIASES
from forven.notification_renderers import summarize_discord_text
from forven.ai import _is_rate_limit_exception, is_transient_provider_exception, normalize_provider_and_model
from forven.task_timeouts import coerce_stale_recovery_minutes

log = logging.getLogger("forven.bot")
_BRAIN_RATE_LIMIT_BACKOFF_SECONDS = (60, 120, 300)
_BRAIN_TRANSIENT_BACKOFF_SECONDS = (120, 300, 900)
_MAX_BRAIN_PROVIDER_RETRIES = 3
_BRAIN_TASK_TIMEOUT_SECONDS = 180
_TASK_WORKER_HEARTBEAT_KEY = "bot:task_worker:last_seen"


def _bot_owns_runtime_loops() -> bool:
    """Whether the Discord gateway should also own scheduler/task loops.

    The API process is the preferred unattended runtime owner because it already
    has singleton locking and fallback workers. Keeping scheduler execution out
    of the Discord event loop prevents long-running scheduled jobs from making
    the gateway appear alive while queue-draining loops are stale.
    """
    return os.environ.get("FORVEN_BOT_OWNS_RUNTIME", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }

# Defaults — overridden by config.json "discord_channels", "discord_owner_id"
_DEFAULT_CHANNELS = {
    "general": "1472929176213393505",
    "ops": "1473714175300603924",
    "approvals": "1473006244171354123",
    "risk": "1473006244171354123",
    "morning-brief": "1473323213143539868",
    "evening-brief": "1473323214083199093",
    "evening-summary": "1473323214083199093",
    "chat": "1473412370528338003",
    "heartbeat": "1473654720735481947",
    "development": "1473714175300603924",
    "strategies": "1473006243147808829",
    "alerts": "1473006244171354123",
    "research": "1473006245211275304",
    "backtesting": "1473036255716577420",
    "paper-trades": "1473036257625112808",
    "market-data": "1473036258962968842",
    "autopilot": "1473036260103815351",
    "news": "1473036261345202340",
    "full-stack-engineer": "1474937376928301169",
    # Backward-compatible aliases that collapse old room-specific names onto the
    # reduced notification/channel model.
    "quant-researcher": "1473006245211275304",
    "back-test-engineer": "1473036255716577420",
    "risk-manager": "1473006244171354123",
    "sentiment": "1473036261345202340",
    "full-stack-engineers": "1473714175300603924",
}


def _load_discord_config() -> tuple[dict, dict, int]:
    """Load channels, reverse lookup, and owner ID from config (or defaults)."""
    cfg = load_config()
    channel_overrides = cfg.get("discord_channels", {}) or {}
    channels = {**_DEFAULT_CHANNELS, **channel_overrides}
    channel_names = {v: k for k, v in channels.items()}
    
    owner_val = cfg.get("discord_owner_id")
    owner_id = int(owner_val) if owner_val else 0
    return channels, channel_names, owner_id


CHANNELS, CHANNEL_NAMES, OWNER_ID = _load_discord_config()

def _load_respond_channels() -> set[str]:
    """Limit conversational bot replies to a small operator-approved set."""
    preferences = kv_get("forven:notification_preferences", {}) or {}
    configured = preferences.get("response_channels") if isinstance(preferences, dict) else None
    if isinstance(configured, list):
        aliases = [str(item).strip() for item in configured if str(item).strip()]
    elif isinstance(configured, str):
        aliases = [item.strip() for item in configured.split(",") if item.strip()]
    else:
        configured = load_config().get("discord_respond_channels")
        if isinstance(configured, list):
            aliases = [str(item).strip() for item in configured if str(item).strip()]
        elif isinstance(configured, str):
            aliases = [item.strip() for item in configured.split(",") if item.strip()]
        else:
            aliases = list(DEFAULT_RESPONSE_CHANNEL_ALIASES)
    return {channel_id for alias, channel_id in CHANNELS.items() if alias in aliases}


# Channels where the bot should respond conversationally (no prefix needed)
RESPOND_CHANNELS = _load_respond_channels()


def refresh_respond_channels() -> set[str]:
    """Refresh the live bot reply-channel whitelist."""
    global RESPOND_CHANNELS
    RESPOND_CHANNELS = _load_respond_channels()
    return RESPOND_CHANNELS


def _owner_guard_enabled() -> bool:
    """Return True when discord_owner_id is configured and should be enforced."""
    return isinstance(OWNER_ID, int) and OWNER_ID > 0


def _is_authorized_operator(author_id: int) -> bool:
    """Validate operator identity for actionable Discord commands.

    Fail closed: if discord_owner_id is not configured, NO Discord user is
    treated as the operator, so the bot won't act on commands from an
    unauthenticated channel. Set discord_owner_id to enable operator commands.
    """
    if _owner_guard_enabled():
        return int(author_id) == int(OWNER_ID)
    return False


def _stale_recovery_minutes() -> int:
    """Resolve stale-task recovery timeout from config with safe lower bounds."""
    settings_payload = {}
    try:
        from forven.db import kv_get
        raw_settings = kv_get("forven:settings", {})
        settings_payload = raw_settings if isinstance(raw_settings, dict) else {}
    except Exception:
        settings_payload = {}

    cfg = load_config()
    raw_value = (
        settings_payload.get("task_stale_recovery_minutes")
        or settings_payload.get("stale_recovery_minutes")
        or cfg.get("task_stale_recovery_minutes")
        or cfg.get("stale_recovery_minutes")
        or cfg.get("agent_task_stale_minutes")
        or 10
    )
    try:
        minutes = int(raw_value)
    except Exception:
        minutes = 0
    return coerce_stale_recovery_minutes(minutes, settings=settings_payload)


def _stale_recovery_interval_seconds(stale_minutes: int) -> float:
    """Throttle stale recovery so it cannot run on every hot loop iteration."""
    return float(max(300, int(stale_minutes) * 60))

# Regex to strip hallucinated tool call XML from AI responses
# MiniMax sometimes generates <minimax:tool_call>, <tool_call>, <invoke>, <function_call>, etc.
# Uses backreference so opening and closing tag names must match.
_TOOL_CALL_RE = re.compile(
    r"<((?:minimax:)?(?:tool_call|tool_use|function_call|invoke))\b[^>]*>"
    r".*?"
    r"</\1>",
    re.DOTALL,
)

_ACTIONABLE_VERB_RE = re.compile(
    r"\b("
    r"create|build|write|implement|fix|update|refactor|set\s*up|configure|"
    r"add|remove|delete|run|execute|deploy|test|backtest|analy[sz]e|research|"
    r"optimi[sz]e|draft|generate|queue|assign|schedule|automate|install"
    r")\b",
    re.IGNORECASE,
)
_ACTIONABLE_REQUEST_RE = re.compile(
    r"\b(can you|could you|would you|will you|please|i need you to|let(?:'|’)s|lets|go ahead and)\b",
    re.IGNORECASE,
)
_NON_ACTIONABLE_OPEN_RE = re.compile(r"^\s*(what|why|when|where|who)\b", re.IGNORECASE)
_AGENT_CALLBACK_TITLE_RE = re.compile(r"completed task ['\"](?P<title>[^'\"]+)['\"]", re.IGNORECASE)


def _sanitize_response(text: str) -> str:
    """Strip hallucinated tool call XML blocks from AI responses.

    MiniMax (and occasionally other models) generates fake XML tool calls
    in plain text when they don't have actual tool access. These look like:
    <minimax:tool_call><invoke name="...">...</invoke></minimax:tool_call>
    """
    cleaned = _TOOL_CALL_RE.sub("", text).strip()
    if not cleaned:
        return "(I tried to use a tool that isn't available in this context. Could you rephrase your question?)"
    return cleaned


def _extract_callback_task_title(message: str) -> str | None:
    """Recover the reviewed task title from the brain callback prompt when possible."""
    match = _AGENT_CALLBACK_TITLE_RE.search(str(message or ""))
    if not match:
        return None
    title = " ".join(str(match.group("title") or "").split()).strip()
    return title or None


def _render_operational_discord_reply(response: str, *, source: str = "", task_message: str = "") -> str:
    """Keep automated Discord task updates terse while preserving user chat replies elsewhere."""
    compact = summarize_discord_text(response, limit=420, max_lines=3) or _single_line(response) or "Forven update."
    if source == "agent_callback":
        task_title = _extract_callback_task_title(task_message)
        if task_title:
            return f"Review complete: {task_title}\n{compact}" if compact.lower() != task_title.lower() else f"Review complete: {task_title}"
    return compact


def _single_line(value: str) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text[:500] if text else None


_BOOTSTRAP_BRAIN_TASK_COOLDOWN_SECONDS = 5 * 60


def should_queue_bootstrap_brain_cycle(*, now: datetime | None = None) -> bool:
    """Return True when the bot should enqueue the startup Brain bootstrap task.

    The gateway bot can reconnect or duplicate processes can slip through during
    local development. Guard against repeated startup Brain tasks by treating any
    recent bootstrap task as an active bootstrap window, regardless of status.
    """
    from forven.db import get_db

    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(seconds=_BOOTSTRAP_BRAIN_TASK_COOLDOWN_SECONDS)).isoformat()

    with get_db() as conn:
        recent = conn.execute(
            """
            SELECT 1
            FROM tasks
            WHERE type = 'brain_invoke'
              AND datetime(created_at) >= datetime(?)
              AND (
                payload LIKE '%"source": "bootstrap"%'
                OR payload LIKE '%"source":"bootstrap"%'
              )
            LIMIT 1
            """,
            (cutoff,),
        ).fetchone()
    return recent is None


_bot_lock_fd: int | None = None

# Lock byte offset — must NOT overlap with PID data written at offset 0.
# On Windows, msvcrt.locking places a mandatory lock that prevents reads of the
# locked region.  Using offset 1024 keeps the PID readable by other processes
# (e.g. start_all.ps1 / get_bot_lock_status) while the bot holds the lock.
_LOCK_BYTE_OFFSET = 1024


def _read_lock_pid(lock_path) -> int | None:
    """Read PID from lock file if present and parseable."""
    try:
        raw = lock_path.read_text().strip()
        if not raw:
            return None
        pid = int(raw)
        return pid if pid > 0 else None
    except Exception:
        return None


def _is_pid_running(pid: int | None) -> bool:
    """Check whether a PID appears to be alive."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return ctypes.GetLastError() == 5  # access denied still means the PID exists
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _is_bot_lock_held(lock_path) -> bool:
    """Check if bot lock is currently held by any process."""
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                return False
            except BlockingIOError:
                return True
        elif msvcrt is not None:
            try:
                os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
                return False
            except (IOError, OSError):
                return True
        else:
            return False
    finally:
        if acquired:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        try:
            os.close(fd)
        except Exception:
            pass


def get_bot_lock_status() -> dict:
    """Return singleton lock health for the bot process."""
    current_pid = os.getpid()
    lock_path = FORVEN_HOME / "bot.lock"
    supported = fcntl is not None or msvcrt is not None

    status = {
        "singleton_supported": supported,
        "singleton_enforced": supported,
        "lock_path": str(lock_path),
        "current_pid": current_pid,
        "held_by_current_process": bool(_bot_lock_fd is not None),
        "lock_held": False,
        "active_pid": None,
        "active_pid_running": False,
        "other_process_active": False,
        "stale_pid": None,
    }

    if not supported:
        status["reason"] = "file locking unavailable"
        return status

    ensure_dirs()
    if _bot_lock_fd is not None:
        status["lock_held"] = True
        status["active_pid"] = current_pid
        status["active_pid_running"] = True
        return status

    lock_held = _is_bot_lock_held(lock_path)
    active_pid = _read_lock_pid(lock_path)
    active_pid_running = _is_pid_running(active_pid)

    status["lock_held"] = bool(lock_held)
    status["active_pid"] = active_pid
    status["active_pid_running"] = bool(active_pid_running)
    status["other_process_active"] = bool(lock_held)
    if active_pid and not active_pid_running and not lock_held:
        status["stale_pid"] = active_pid
        status["active_pid"] = None

    return status


def _acquire_bot_lock() -> bool:
    """Acquire cross-process singleton lock for the Discord bot."""
    global _bot_lock_fd

    if _bot_lock_fd is not None:
        return True

    ensure_dirs()
    lock_path = FORVEN_HOME / "bot.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)

    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
    elif msvcrt is not None:
        try:
            os.lseek(fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except (IOError, OSError):
            os.close(fd)
            return False
    else:
        os.close(fd)
        raise RuntimeError("No file locking available (fcntl or msvcrt required); cannot safely acquire bot singleton lock.")

    # Write PID at offset 0 (readable by other processes since lock is at offset 1024)
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    _bot_lock_fd = fd
    return True


def _release_bot_lock():
    """Release singleton bot lock."""
    global _bot_lock_fd

    if _bot_lock_fd is None:
        return
    try:
        os.ftruncate(_bot_lock_fd, 0)
    except Exception:
        pass
    try:
        if fcntl is not None:
            fcntl.flock(_bot_lock_fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(_bot_lock_fd, _LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(_bot_lock_fd, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        os.close(_bot_lock_fd)
    except Exception:
        pass
    _bot_lock_fd = None


def get_bot_token() -> str:
    """Get Discord bot token from environment or config. Never hardcoded."""
    token = os.environ.get("DISCORD_TOKEN")
    if token:
        return token
    cfg = load_config()
    # Stored encrypted by the Settings save path (audit P1.5); decrypt_secret
    # passes any legacy plaintext token through unchanged for backward compat.
    from forven.secret_storage import decrypt_secret
    token = decrypt_secret(cfg.get("discord_token") or "")
    if not token:
        raise ValueError(
            "Discord bot token not found. Set DISCORD_TOKEN env var "
            "or add 'discord_token' to ~/.forven/config.json"
        )
    return token


def _build_default_agents() -> list[dict]:
    """Build the DEFAULT_AGENTS seed list, using dynamic settings from kv."""
    from forven.db import kv_get
    from forven.roster import LIVE_AGENTS

    settings = kv_get("forven:settings", {})
    pipeline = kv_get("forven:pipeline_thresholds", {})

    max_dd = settings.get("max_drawdown_pct", 10)
    daily_loss = settings.get("max_daily_loss", 500)
    max_trade = settings.get("max_position_size_pct", 2)

    live_cfg = pipeline.get("live_graduated", {})
    if not isinstance(live_cfg, dict):
        live_cfg = {}
    decay_threshold = live_cfg.get(
        "decay_kill_switch_pct",
        pipeline.get("decay", {}).get("degradation_threshold", 0.30),
    )
    if decay_threshold > 1.0:
        decay_threshold /= 100.0
    decay_pct = int(decay_threshold * 100)
    decay_window = int(pipeline.get("decay", {}).get("window_hours", 72))

    return [
        {
            "agent_id": "quant-researcher",
            "name": LIVE_AGENTS["quant-researcher"]["name"],
            "role": "Research market structure, benchmark external ideas, identify missing data or feature gaps, and own data integrity, feature reliability, and dataset drift/decay checks.",
            "model": "openai",
            "model_id": get_default_model_for_provider("openai"),
            "visibility": "visible",
            "instructions": (
                "You are the Quant Researcher for Forven. Your focus is benchmarking, market structure research, data-gap discovery, and data integrity.\n"
                "1. Read LESSONS.md and archived failure patterns before proposing anything.\n"
                "2. Search ChromaDB (backtest_results, research_hypotheses, post-mortems) for useful precedents and missing coverage.\n"
                "3. Analyze the current market regime, external sources, and exploitable edges.\n"
                "4. Surface benchmark candidates, market observations, and data gaps that strategy-developer agents can use.\n"
                "5. For any assigned post_mortem task, produce an explicit failure diagnosis with metric evidence, breached thresholds, and corrective actions.\n"
                "6. Store each failure post-mortem in ChromaDB trade_post_mortems and summarize guardrails as agent narratives.\n"
                "7. Own data integrity and feature reliability: validate feature definitions, audit dataset drift, feature decay, gaps, and outliers before strategy changes ship; treat data quality as a first-class risk factor.\n"
                "TRADE FREQUENCY: Every strategy must generate at least 30 trades/year. 4h charts: ~1 entry per 12 days. 1h charts: ~1 entry per 3 days. Fewer than 10/year fails WFA.\n"
                "FILTER DISCIPLINE: Use at most 2 entry filters simultaneously (primary signal + one confirmation). Never stack 3+ conditions — it collapses trade frequency.\n"
                "ADX LIMITS: adx_min must not exceed 30 on 1h/4h. If using adx_min AND adx_max, they must differ by ≥15 points.\n"
                "WIDE ENTRY ZONES: RSI at 35/65 not 40/60. Stochastic at 25/75 not 30/70. Tight zones kill trade frequency.\n"
                "Always reference the relevant hypothesis or Strategy Container ID explicitly in your output."
            ),
        },
        {
            "agent_id": "simulation-agent",
            "name": LIVE_AGENTS["simulation-agent"]["name"],
            "role": "Stress-test strategy hypotheses with Walk-Forward Analysis, Monte Carlo simulation, and parameter optimization. Validate that strategies are robust, not curve-fitted.",
            "model": "openai",
            "model_id": get_default_model_for_provider("openai"),
            "visibility": "visible",
            "instructions": (
                "You are the Simulation Agent for Forven. Your focus is Strategy Container Validation.\n"
                "No backtest, optimization, or Walk-Forward Analysis can exist outside a Strategy Container.\n"
                "Input is mandatory: a specific Strategy Container ID S0000X in Test stage.\n"
                "1. Confirm the provided Strategy Container ID S0000X and Test stage before running validation.\n"
                "2. Run the robustness gauntlet for that Strategy Container only.\n"
                "3. Save all backtest, optimization, and Walk-Forward metrics strictly as historical events on that Strategy Container.\n"
                "4. Use the container tabbed history to build the final evidence summary.\n"
                "5. Recommend to the Brain whether the Strategy Container passes and should be promoted to Paper.\n"
                "Always tag every validation artifact with Strategy Container ID S0000X."
            ),
        },
        {
            "agent_id": "risk-manager",
            "name": LIVE_AGENTS["risk-manager"]["name"],
            "role": f"Monitor portfolio risk, review position sizing, evaluate strategy health, enforce capital preservation rules. {max_dd}% drawdown kill, ${daily_loss} daily loss, {max_trade}% per trade.",
            "model": "openai",
            "model_id": get_default_model_for_provider("openai"),
            "visibility": "visible",
            "instructions": (
                "You are the Risk Manager for Forven. Your focus is Live Strategy Container Oversight and Capital Allocation.\n"
                "You own merged capital-allocation and risk-budgeting duties.\n"
                f"Rules: {max_dd}% max drawdown kill switch, ${daily_loss} daily loss halt, {max_trade}% max per trade.\n"
                "1. Monitor the Execution tab for every Strategy Container in Paper or Live stage.\n"
                "2. Allocate capital multipliers using each container's historical Walk-Forward performance versus live correlation.\n"
                "3. Enforce concentration controls and position sizing bounds across all active Strategy Containers.\n"
                f"4. Enforce kill switch: if {decay_window}h live Sharpe/drawdown degrades >{decay_pct}% versus baseline, autonomously demote the Strategy Container to Test or Archived.\n"
                "5. Record allocation, demotion, and kill-switch decisions against the Strategy Container history.\n"
                "Always reference Strategy Container ID S0000X when issuing risk or allocation directives."
            ),
        },
        # execution-trader RETIRED (2026-06-30): trade execution is owned by the
        # scanner's parity kernel (manage_positions_via_kernel) and the operator's
        # manual position controls. There is no LLM-driven order path anymore — an
        # agent placing/closing orders out-of-band fabricated phantom/unknown
        # closes. The row is deleted via `deprecated_agents` in seed_default_agents.
        {
            "agent_id": "strategy-developer",
            "name": LIVE_AGENTS["strategy-developer"]["name"],
            "role": "Generate market hypotheses and translate them directly into testable Strategy Container logic.",
            "model": "openai",
            "model_id": get_default_model_for_provider("openai"),
            "visibility": "visible",
            "instructions": (
                "You are the Strategy Developer for Forven. You own the full hypothesis-to-strategy loop.\n"
                "1. Generate first-class hypotheses from market evidence, priors, and assigned research lanes.\n"
                "2. When a hypothesis is specific enough, immediately spawn one or more initial Strategy Container candidates from it.\n"
                "3. Keep each hypothesis and each strategy linked through the hypothesis-first pipeline.\n"
                "4. Implement Python logic and indicators for each Strategy Container candidate you create or inherit.\n"
                "5. Update the specific Strategy Container configuration payload with logic, parameters, and indicator settings.\n"
                "6. Keep all strategy logic inside the Strategy Container lifecycle; do not treat strategies as standalone files.\n"
                "7. Return hypothesis-scoped and container-scoped summaries keyed by the relevant IDs."
            ),
        },
        {
            "agent_id": "full-stack-engineer",
            "name": LIVE_AGENTS["full-stack-engineer"]["name"],
            "role": "Operator-triggered diagnosis and triage for bug reports, approval troubleshooting, and notification-repair requests. The autonomous code-execution path is retired: this agent investigates and reports; it does not modify code.",
            "model": "openai",
            "model_id": get_default_model_for_provider("openai"),
            "visibility": "visible",
            "instructions": (
                "You are the Full-Stack Engineer for Forven — the operator-facing triage and diagnosis agent.\n"
                "The autonomous code-execution path is RETIRED. You investigate and report; you do NOT modify code, open PRs, or create approvals.\n"
                "1. For a bug-report, approval-troubleshoot, or notification-repair task, reproduce and localize the fault using read-only inspection (read_file, logs, run_code for diagnosis only).\n"
                "2. Produce a clear root-cause diagnosis: the failing component, the supporting evidence, and a concrete recommended fix for a human / Claude Code to apply.\n"
                "3. Never claim a fix was applied — code changes go through the normal human review + tests workflow, not this agent.\n"
                "4. Summarize your findings in the task output so the operator can act."
            ),
        },
        {
            "agent_id": "brain",
            "name": LIVE_AGENTS["brain"]["name"],
            "role": "Central orchestrator and decision layer for Forven. Delegates work and arbitrates between agents.",
            "model": "openai",
            "model_id": get_default_model_for_provider("openai"),
            "visibility": "visible",
            "instructions": (
                "You are the Brain for Forven. Your focus is Strategy Container Lifecycle Management.\n"
                "A Strategy is an immutable Strategy Container with ID format S0000X and canonical label [ASSET]-[TYPE]-S[ID].\n"
                "Lifecycle is strict: Ideation -> Test -> Paper -> Live.\n"
                "1. Assign work by Strategy Container ID S0000X only; every delegated task must include the specific container ID.\n"
                "2. Strategy-developer agents are the primary hypothesis creators and may immediately spawn Strategy Container candidates from their own hypotheses.\n"
                "3. Enforce lifecycle transitions using evidence from container history; reject non-container workflows.\n"
                "4. Keep all strategy decisions scoped to immutable Strategy Container records, never loose files or abstract strategy ideas.\n"
                "Always require explicit Strategy Container ID S0000X in planning, execution, and reporting."
            ),
        },
    ]


def seed_default_agents() -> dict:
    """Seed core Forven agents into the DB. Idempotent; safe to call on every startup.

    Runs independently of the Discord bot connection, so the UI has agent rows
    to operate on even when DISCORD_TOKEN is unset.
    """
    from forven.db import get_db
    from forven.agents.manager import (
        create_agent,
        delete_agent,
        ensure_agent_identity_files,
        update_agent,
    )

    default_agents = _build_default_agents()

    with get_db() as conn:
        existing = {r["id"] for r in conn.execute("SELECT id FROM agents").fetchall()}

    removed_deprecated: list[str] = []
    from forven.roster import DEPRECATED_AGENT_IDS as deprecated_agents

    for deprecated_id in sorted(deprecated_agents & existing):
        try:
            delete_agent(deprecated_id)
            existing.discard(deprecated_id)
            removed_deprecated.append(deprecated_id)
        except Exception as e:
            log.warning("Deprecated agent removal failed for %s: %s", deprecated_id, e)

    created: list[str] = []
    updated: list[str] = []
    healed: list[str] = []
    for agent_def in default_agents:
        agent_id = agent_def["agent_id"]
        if agent_id not in existing:
            try:
                create_agent(**agent_def)
                created.append(agent_id)
            except Exception as e:
                log.warning("Agent creation failed for %s: %s", agent_id, e)
        else:
            try:
                update_agent(
                    agent_id,
                    role=agent_def["role"],
                    visibility=agent_def.get("visibility", "visible"),
                    instructions=agent_def["instructions"],
                )
                updated.append(agent_id)
            except Exception as e:
                log.warning("Agent update failed for %s: %s", agent_id, e)
        # Self-heal: (re)create any MISSING per-agent identity files
        # (SOUL.md/AGENTS.md/ROLE.md) for every built-in agent — including
        # ones that already existed in the DB (update_agent writes no files).
        # ensure_agent_identity_files never overwrites non-empty content.
        try:
            if ensure_agent_identity_files(
                agent_id,
                agent_def["name"],
                agent_def["role"],
                agent_def.get("instructions"),
            ):
                healed.append(agent_id)
        except Exception as e:
            log.warning("Agent identity-file seeding failed for %s: %s", agent_id, e)

    return {
        "created": created,
        "updated": updated,
        "healed": healed,
        "removed_deprecated": removed_deprecated,
        "total": len(default_agents),
    }


class ForvenBot(commands.Bot):
    """Forven Discord bot — gateway to the AI brain."""

    def __init__(self, agent_id: str | None = None):
        intents = discord.Intents.default()
        self.agent_id = agent_id
        self._agent_data: dict | None = None
        intents.message_content = True
        intents.members = True
        # SECURITY (audit 2026-06-22, L10): suppress @everyone/@here/role pings
        # bot-wide. LLM-generated message text could otherwise mass-ping the
        # operator's server. Applies to every .send() unless explicitly overridden.
        super().__init__(
            command_prefix="!",
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self._ready_event = asyncio.Event()
        # Conversation history per channel (last N messages for context)
        self._history: dict[str, list[dict]] = {}
        self._history_limit = 20
        # Track in-flight tasks so stale-recovery never re-queues active work.
        self._active_agent_task_ids: set[int] = set()
        self._active_brain_task_ids: set[int] = set()
        self._last_stale_recovery_at: float = 0.0
        self._stale_recovery_lock = asyncio.Lock()

    async def setup_hook(self):
        """Called when bot is starting — start background tasks."""
        if not self.agent_id and _bot_owns_runtime_loops():
            self.scheduler_loop.start()
            self.task_processor_loop.start()
            self.agent_runner_loop.start()
            self.ops_queue_recovery_loop.start()
        elif not self.agent_id:
            log.info(
                "Bot runtime loops disabled; API runtime owns scheduler and task queues "
                "(set FORVEN_BOT_OWNS_RUNTIME=1 to override)."
            )

    async def on_ready(self):
        log.info("Bot connected as %s (ID: %s, Agent: %s)", self.user.name, self.user.id, self.agent_id)
        if not _owner_guard_enabled():
            log.warning(
                "discord_owner_id is not configured; owner-only Discord command guard is disabled."
            )
        if self.agent_id:
            self._agent_data = self._load_agent_data()
            self._ready_event.set()
            return
        self._ready_event.set()

        # Initialize database. Runtime recovery/bootstrap is intentionally
        # reserved for the runtime owner (normally the API process); a
        # gateway-only Discord bot must not clear scheduler locks or requeue
        # work that the API scheduler currently owns.
        from forven.db import init_db

        init_db()
        if not _bot_owns_runtime_loops():
            try:
                seed_default_agents()
            except Exception as exc:
                log.warning("Gateway-only agent seed failed: %s", exc)
            log.info("Bot gateway ready; runtime recovery/bootstrap skipped because API owns runtime.")
            return

        from forven.db import (
            STALE_RECOVERY_FAIL_AGENTS,
            recover_dangling_runtime_tasks,
            recover_stale_running_tasks,
        )
        from forven.scheduler import reset_scheduler_job_locks
        from forven.system_mode_policy import reconcile_manual_mode_backlog

        recovered_dangling = recover_dangling_runtime_tasks()
        if any(recovered_dangling.values()):
            log.info("Recovered dangling runtime tasks at startup: %s", recovered_dangling)
        manual_counts = reconcile_manual_mode_backlog()
        if manual_counts.get("total"):
            log.info("Reconciled manual-mode backlog at startup: %s", manual_counts)
        recovered_job_locks = reset_scheduler_job_locks()
        if recovered_job_locks:
            log.info("Cleared %d inherited scheduler job lock(s) at startup", recovered_job_locks)
        recovered = recover_stale_running_tasks(
            stale_minutes=_stale_recovery_minutes(),
            fail_agents=STALE_RECOVERY_FAIL_AGENTS,
        )
        if any(recovered.values()):
            log.info("Recovered stale tasks at startup: %s", recovered)
        self._last_stale_recovery_at = time.time()

        # Bootstrap: seed agents, jobs, workspace, kick brain
        await self._bootstrap()

    def _has_inflight_task_work(self) -> bool:
        return bool(self._active_agent_task_ids or self._active_brain_task_ids)

    def _record_task_worker_heartbeat(self, loop_name: str) -> None:
        if self.agent_id:
            return
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            existing = kv_get(_TASK_WORKER_HEARTBEAT_KEY, {}) or {}
            existing_loops = existing.get("loops") if isinstance(existing, dict) else {}
            loops = dict(existing_loops) if isinstance(existing_loops, dict) else {}
            loops[str(loop_name or "unknown")] = now_iso
            kv_set_best_effort(
                _TASK_WORKER_HEARTBEAT_KEY,
                {
                    "pid": os.getpid(),
                    "loop": loop_name,
                    "loops": loops,
                    "updated_at": now_iso,
                },
            )
        except Exception:
            log.debug("Task-worker heartbeat write failed", exc_info=True)

    async def _maybe_recover_stale_tasks(self, source: str) -> None:
        """Run stale-task recovery opportunistically, never while work is inflight."""
        if self.agent_id:
            return

        stale_minutes = _stale_recovery_minutes()
        interval_seconds = _stale_recovery_interval_seconds(stale_minutes)
        now = time.time()
        if now - self._last_stale_recovery_at < interval_seconds:
            return
        if self._has_inflight_task_work():
            return

        async with self._stale_recovery_lock:
            now = time.time()
            if now - self._last_stale_recovery_at < interval_seconds:
                return
            if self._has_inflight_task_work():
                return

            from forven.db import STALE_RECOVERY_FAIL_AGENTS, recover_stale_running_tasks

            recovered = await asyncio.to_thread(
                recover_stale_running_tasks,
                stale_minutes=stale_minutes,
                fail_agents=STALE_RECOVERY_FAIL_AGENTS,
            )
            self._last_stale_recovery_at = now
            if any(recovered.values()):
                log.info("Recovered stale tasks in %s loop: %s", source, recovered)

    def _load_agent_data(self) -> dict | None:
        """Load this bot's agent configuration from DB."""
        if not self.agent_id:
            return None

        from forven.db import get_db

        try:
            with get_db() as conn:
                row = conn.execute(
                    "SELECT id, name, role, model, model_id, instructions FROM agents WHERE id = ?",
                    (self.agent_id,),
                ).fetchone()
        except Exception as e:
            log.warning("Failed loading agent data for %s: %s", self.agent_id, e)
            return None

        if not row:
            log.warning("Agent bot %s connected but no DB row found", self.agent_id)
            return None

        return dict(row)

    async def _bootstrap(self):
        """One-time bootstrap: seed agents, scheduler jobs, workspace, then kick Brain.

        Safe to call multiple times — updates agent roles/instructions with latest settings.
        """
        from forven.db import get_db, log_activity
        from forven.scheduler import (
            seed_forven_jobs,
            get_jobs,
            ensure_monitoring_jobs,
            reconcile_forven_jobs,
        )
        from forven.workspace import init_workspace

        boot_actions = []

        # 1. Initialize workspace identity files (SOUL.md, etc.)
        try:
            init_workspace()
            boot_actions.append("workspace initialized")
        except Exception as e:
            log.warning("Workspace init: %s", e)

        # 2. Seed default agents if they don't exist (idempotent, also callable
        # outside the Discord on_ready path so the UI has rows even when the
        # bot can't connect).
        seed_result = seed_default_agents()
        if seed_result["removed_deprecated"]:
            boot_actions.append(
                f"removed deprecated agents: {', '.join(seed_result['removed_deprecated'])}"
            )
        if seed_result["created"]:
            boot_actions.append(
                f"created {len(seed_result['created'])} agents: {', '.join(seed_result['created'])}"
            )
        else:
            boot_actions.append(f"all {seed_result['total']} agents updated")

        # 3. Seed scheduler jobs if none exist
        existing_jobs = get_jobs()
        if not existing_jobs:
            seed_forven_jobs()
            boot_actions.append("seeded 10 scheduler jobs")
        else:
            boot_actions.append(f"{len(existing_jobs)} scheduler jobs exist")
            reconciliation = reconcile_forven_jobs()
            if reconciliation["removed"]:
                boot_actions.append(f"removed {reconciliation['removed']} stale scheduler jobs")
            if reconciliation["added"]:
                boot_actions.append(f"restored {reconciliation['added']} missing scheduler jobs")
            added_monitoring = ensure_monitoring_jobs()
            if added_monitoring:
                boot_actions.append(f"added {added_monitoring} monitoring scheduler jobs")

        # 4. Queue initial Brain cycle to kick off orchestration
        with get_db() as conn:
            if should_queue_bootstrap_brain_cycle():
                create_pending_task(
                    conn,
                    "brain_invoke",
                    {
                        "source": "bootstrap",
                        "message": (
                            "Forven just started. You are the Brain — the sole orchestrator.\n\n"
                            "Review the current state of the system:\n"
                            "1. Check what Strategy Containers exist and their lifecycle statuses\n"
                            "2. Review any pending post-mortems from closed trades\n"
                            "3. Check the market regime and sentiment\n"
                    "4. Assign tasks to your agents as needed:\n"
                    "   - strategy-developer swarm: Generate first-class hypotheses and spawn initial strategy candidates immediately\n"
                    "   - defer quant-researcher until after the first strategy-developer hypothesis wave is underway; then use it only for external benchmarks, market structure, missing data, and data-quality/feature-integrity checks\n"
                    "   - simulation-agent: Validate Strategy Containers in Test stage\n"
                            "   - risk-manager: Oversee paper + live container risk and capital allocation (trade execution is automated by the scanner kernel — there is no execution agent)\n"
                            "   - brain: Escalate orchestrator-level control tasks if needed\n"
                            "5. Update LESSONS.md with any new insights\n\n"
                            "Be proactive. Don't wait — assign work NOW."
                        ),
                        "channel": "research",
                    },
                    priority=1,
                    source="system",
                )
                boot_actions.append("queued initial brain cycle")
            else:
                boot_actions.append("bootstrap brain cycle already queued recently")

        # 5. Announce bootstrap to Discord
        summary = "\n".join(f"  - {a}" for a in boot_actions)
        boot_msg = f"FORVEN BOOTSTRAP COMPLETE\n{summary}"
        log.info(boot_msg)
        log_activity("info", "bot", boot_msg)

        try:
            channel = self.get_channel(int(CHANNELS.get("general", "0")))
            if channel:
                await channel.send(f"```\n{boot_msg}\n```")
        except Exception as e:
            log.warning("Bootstrap Discord announce failed: %s", e)

    async def on_message(self, message: discord.Message):
        # Ignore own messages
        if message.author.id == self.user.id:
            return

        # Agent bots: direct persona chat (no tools, no command handling)
        if self.agent_id:
            mentioned = False
            if self.user:
                try:
                    mentioned = self.user.mentioned_in(message)
                except Exception:
                    mentioned = any(getattr(m, "id", None) == self.user.id for m in getattr(message, "mentions", []))

            # Prevent bot-to-bot chatter loops:
            # - bot-authored messages are handled only when this bot is explicitly @mentioned.
            # - human/operator messages still use owner guard rules.
            if message.author.bot and not mentioned:
                return
            if not message.author.bot and not _is_authorized_operator(message.author.id):
                return
            if message.content.startswith("!"):
                return

            content = message.content.strip()
            if not content:
                return

            is_dm = isinstance(message.channel, discord.DMChannel)
            channel_id = str(message.channel.id)
            channel_name = CHANNEL_NAMES.get(channel_id, "DM" if is_dm else "unknown")

            # Room isolation: agent bots respond only in their mapped room, unless DM or explicitly mentioned.
            try:
                from forven.reporter import AGENT_CHANNEL_MAP

                assigned_channel = AGENT_CHANNEL_MAP.get(self.agent_id or "", "")
            except Exception:
                assigned_channel = ""
            assigned_channel_id = str(CHANNELS.get(assigned_channel, "")) if assigned_channel else ""
            if not is_dm:
                if assigned_channel_id and channel_id != assigned_channel_id and not mentioned:
                    return
                if not assigned_channel_id and not mentioned:
                    return

            if self._is_actionable_request(content):
                try:
                    await self._auto_assign_agent_task(message, content, channel_id, channel_name)
                except Exception as e:
                    log.error("Auto-assign failed (%s): %s", self.agent_id, e, exc_info=True)
                    await message.channel.send(f"Error queuing task: {str(e)[:200]}")
                return

            log.info("Agent message from Judder to %s in #%s: %s", self.agent_id, channel_name, content[:100])

            async with message.channel.typing():
                try:
                    response = await self._generate_agent_response(content, channel_id, channel_name)
                    await self._send_response(message.channel, response)

                    from forven.db import log_activity

                    log_activity(
                        "info",
                        f"agent-chat:{self.agent_id}",
                        f"[#{channel_name}] Judder: {content[:100]} → Response: {response[:100]}",
                    )
                except Exception as e:
                    log.error("Agent response generation failed (%s): %s", self.agent_id, e, exc_info=True)
                    if _is_rate_limit_exception(e):
                        await message.channel.send(
                            "Provider rate limit hit. I’m pausing and will retry when the next queue cycle runs. Try again in ~30s."
                        )
                    else:
                        await message.channel.send(f"Error: {str(e)[:200]}")
            return

        # Main bot never responds to other bots to avoid cross-bot loops.
        if message.author.bot:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        channel_id = str(message.channel.id)
        is_allowed_channel = is_dm or channel_id in RESPOND_CHANNELS

        # Honor explicit command prefix before normal chat handling.
        # This is required so operators can route work into task queues.
        if message.content.startswith("!") and is_allowed_channel:
            await self.process_commands(message)
            return

        # Only process messages from Judder in allowed channels/DMs
        if not _is_authorized_operator(message.author.id):
            return
        if not is_allowed_channel:
            return

        content = message.content.strip()
        if not content:
            return

        channel_name = CHANNEL_NAMES.get(channel_id, "DM" if is_dm else "unknown")

        # All channels (including #general) use the same routing:
        # actionable requests → brain task queue, everything else → conversational chat.
        # Use `!brain <message>` for explicit brain tasks from any channel.
        if self._is_actionable_request(content):
            try:
                await self._auto_queue_brain_task(message, content, channel_id, is_dm)
            except Exception as e:
                log.error("Auto brain-queue failed: %s", e, exc_info=True)
                await message.channel.send(f"Error queuing brain task: {str(e)[:200]}")
            return

        log.info("Message from Judder in #%s: %s", channel_name, content[:100])

        # Show typing indicator while processing
        async with message.channel.typing():
            try:
                response = await self._generate_response(content, channel_id, channel_name, is_dm)
                await self._send_response(message.channel, response)

                # Log to activity
                from forven.db import log_activity
                log_activity("info", "bot", f"[#{channel_name}] Judder: {content[:100]} → Response: {response[:100]}")

            except Exception as e:
                log.error("Response generation failed: %s", e, exc_info=True)
                if _is_rate_limit_exception(e):
                    await message.channel.send(
                        "Provider rate limit hit. I’m pausing and will retry when the next queue cycle runs. Try again in ~30s."
                    )
                else:
                    await message.channel.send(f"Error: {str(e)[:200]}")

                await self.process_commands(message)

    @commands.command(name="brain")
    async def cmd_brain(self, ctx, *, message: str = ""):
        """Queue a Brain cycle from a direct operator message."""
        if not _is_authorized_operator(ctx.author.id):
            return
        if not isinstance(ctx.channel, discord.DMChannel) and str(ctx.channel.id) not in RESPOND_CHANNELS:
            return

        if not message.strip():
            await ctx.send("Usage: `!brain <message>` — queues a brain cycle task.")
            return

        from forven.db import get_db

        match = re.search(r"<#(\d+)>", message)
        if match:
            channel_id = match.group(1)
            message = message.replace(match.group(0), "").strip()
        else:
            channel_id = str(ctx.channel.id)

        payload = {
            "source": "discord_command",
            "message": message.strip(),
            "channel": channel_id,
        }

        with get_db() as conn:
            create_pending_task(
                conn,
                "brain_invoke",
                payload,
                priority=1,
                source="user",
            )

        await ctx.send("Queued brain task ✅")

    @commands.command(name="assign")
    async def cmd_assign(self, ctx, *, message: str = ""):
        """Assign a direct task to an agent: `!assign <agent-id>: <task>`"""
        if not _is_authorized_operator(ctx.author.id):
            return
        if not isinstance(ctx.channel, discord.DMChannel) and str(ctx.channel.id) not in RESPOND_CHANNELS:
            return

        if not message.strip():
            await ctx.send("Usage: `!assign <agent-id>: <title and details>`")
            return

        if ":" in message:
            agent_id, task_text = message.split(":", 1)
            agent_id = agent_id.strip()
            task_text = task_text.strip()
            title = task_text.split("\n", 1)[0].strip()[:80] if task_text else "Manual task"
        else:
            parts = message.strip().split(maxsplit=1)
            if len(parts) < 2:
                await ctx.send("Usage: `!assign <agent-id>: <task>` (agent-id and task required)")
                return
            agent_id, task_text = parts[0].strip(), parts[1].strip()
            title = task_text[:80]

        if not task_text:
            await ctx.send("Task body is required. Example: `!assign full-stack-engineer: build scheduler monitor`")
            return

        from forven.db import get_db
        from forven.brain import assign_task

        match = re.search(r"<#(\d+)>", task_text)
        if match:
            channel_id = match.group(1)
            task_text = task_text.replace(match.group(0), "").strip()
        else:
            channel_id = str(ctx.channel.id)

        with get_db() as conn:
            exists = conn.execute("SELECT 1 FROM agents WHERE id = ?", (agent_id,)).fetchone()
        if not exists:
            await ctx.send(f"Unknown agent `{agent_id}`")
            return

        assign_task(
            agent_id=agent_id,
            task_type="manual",
            title=title or "Manual task",
            description=task_text,
            input_data={"_channel": channel_id},
            source="user",
        )
        await ctx.send(f"Queued task for `{agent_id}` ✅")

    @commands.command(name="engineer", aliases=["fullstack"])
    async def cmd_engineer(self, ctx, *, message: str = ""):
        """Shortcut to assign a task to the full-stack engineer."""
        if not _is_authorized_operator(ctx.author.id):
            return
        if not isinstance(ctx.channel, discord.DMChannel) and str(ctx.channel.id) not in RESPOND_CHANNELS:
            return

        if not message.strip():
            await ctx.send("Usage: `!engineer <task>`")
            return

        from forven.brain import assign_task

        match = re.search(r"<#(\d+)>", message)
        if match:
            channel_id = match.group(1)
            message = message.replace(match.group(0), "").strip()
        else:
            channel_id = str(ctx.channel.id)

        title = message.strip().split("\n", 1)[0].strip()[:80]
        assign_task(
            agent_id="full-stack-engineer",
            task_type="manual",
            title=title or "Manual full-stack task",
            description=message.strip(),
            input_data={"_channel": channel_id},
            source="user",
        )
        await ctx.send("Queued task for full-stack engineer ✅")

    def _is_actionable_request(self, content: str) -> bool:
        """Heuristic: decide whether a Discord chat message is a task request."""
        text = (content or "").strip()
        if not text:
            return False

        lowered = text.lower()
        has_action_verb = bool(_ACTIONABLE_VERB_RE.search(lowered))
        has_request_frame = bool(_ACTIONABLE_REQUEST_RE.search(lowered))

        if _NON_ACTIONABLE_OPEN_RE.match(lowered) and not has_request_frame:
            return False
        if lowered.startswith("how ") and not has_request_frame and not has_action_verb:
            return False
        if text.endswith("?") and not has_request_frame and not has_action_verb:
            return False

        return has_action_verb

    def _task_title_from_text(self, content: str) -> str:
        title = (content or "").strip().split("\n", 1)[0].strip()
        return title[:80] if title else "Manual task"

    async def _auto_queue_brain_task(
        self,
        message: discord.Message,
        content: str,
        channel_id: str,
        is_dm: bool,
    ) -> None:
        from forven.db import get_db, log_activity

        payload = {
            "source": "discord_auto_actionable",
            "message": content.strip(),
            "channel": channel_id,
        }

        with get_db() as conn:
            create_pending_task(
                conn,
                "brain_invoke",
                payload,
                priority=1,
                source="user",
            )

        channel_name = CHANNEL_NAMES.get(channel_id, "DM" if is_dm else "unknown")
        log_activity(
            "info",
            "bot",
            f"[auto-queue #{channel_name}] {content[:120]}",
            {"channel_id": channel_id, "source": "discord_auto_actionable"},
        )
        await message.channel.send("Actionable request detected. Queued brain task ✅")

    async def _auto_assign_agent_task(
        self,
        message: discord.Message,
        content: str,
        channel_id: str,
        channel_name: str,
    ) -> None:
        from forven.brain import assign_task
        from forven.db import log_activity

        agent_id = str(self.agent_id or "").strip()
        if not agent_id:
            raise RuntimeError("Agent ID missing for auto-assignment")

        assign_task(
            agent_id=agent_id,
            task_type="manual",
            title=self._task_title_from_text(content),
            description=content.strip(),
            input_data={"_channel": channel_id, "source": "discord_auto_actionable"},
            source="user",
        )
        log_activity(
            "info",
            f"agent-chat:{agent_id}",
            f"[auto-assign #{channel_name}] {content[:120]}",
            {"channel_id": channel_id, "source": "discord_auto_actionable"},
        )
        await message.channel.send(f"Actionable request detected. Queued task for `{agent_id}` ✅")

    async def _generate_agent_response(self, content: str, channel_id: str, channel_name: str) -> str:
        """Generate an AI response for an agent-specific Discord bot."""
        from forven.ai import call_ai
        from forven.context import build_agent_context
        from forven.vectordb import store_narrative
        from forven.workspace import append_workspace, read_workspace

        agent = self._agent_data
        if not agent:
            agent = self._load_agent_data()
            self._agent_data = agent
        if not agent:
            raise RuntimeError(f"Agent config not found for {self.agent_id}")

        agent_id = str(agent.get("id") or self.agent_id or "").strip()
        agent_name = str(agent.get("name") or agent_id).strip()
        agent_role = str(agent.get("role") or "").strip()
        if not agent_id:
            raise RuntimeError("Agent ID missing in agent bot context")

        role_md = read_workspace(f"agents/{agent_id}/ROLE.md", optional=True)
        if not role_md:
            role_md = str(agent.get("instructions") or agent_role or "").strip()

        context = build_agent_context(
            agent_id,
            role_md,
            content,
            include_daily_memory=True,
        )

        preamble = (
            f"You are {agent_name}, a specialist agent in the Forven team.\n"
            f"Your role: {agent_role or 'Specialist operator.'}\n"
            "Stay in this persona and never answer as Forven (the Brain)."
        )
        context = preamble + "\n\n---\n\n" + context

        history = self._history.get(channel_id, [])
        if history:
            context += "\n\n---\n\n# RECENT CONVERSATION IN THIS CHANNEL\n"
            for msg in history[-10:]:
                role = "Judder" if msg["role"] == "user" else agent_name
                context += f"\n**{role}**: {msg['content'][:300]}\n"

        provider, model = normalize_provider_and_model(
            str(agent.get("model") or ""),
            str(agent.get("model_id") or ""),
        )

        context += (
            "\n\n---\n\n# CHAT MODE\n"
            f"You are in CHAT mode responding to Judder as {agent_name} on Discord.\n"
            "You do NOT have tools, function calls, or API access. Do NOT generate XML, "
            "tool_call blocks, invoke tags, or any structured function calls.\n"
            "Answer directly in plain text from the context and your role.\n"
            "Actionable requests are auto-queued by the Discord gateway when detected.\n"
            f"If an action request reaches you without being queued, tell Judder to use `!assign {agent_id}: <task>` on the main Forven bot.\n"
            f"End every response with the signature line: -- {agent_name}"
        )

        response = await call_ai(
            provider=provider,
            model=model,
            prompt=content,
            system=context,
            max_tokens=4096,
            temperature=0.7,
        )

        if channel_id not in self._history:
            self._history[channel_id] = []
        self._history[channel_id].append({"role": "user", "content": content})
        self._history[channel_id].append({"role": "assistant", "content": response})
        if len(self._history[channel_id]) > self._history_limit * 2:
            self._history[channel_id] = self._history[channel_id][-self._history_limit * 2:]

        today = datetime.now(timezone.utc).date().isoformat()
        try:
            append_workspace(
                f"agents/{agent_id}/memory/{today}.md",
                f"\n### Discord #{channel_name} — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
                f"**Judder**: {content[:200]}\n**{agent_name}**: {response[:200]}\n",
            )
        except Exception as e:
            log.warning("Agent memory append failed (%s): %s", agent_id, e)

        try:
            summary = f"[Discord agent:{agent_id} #{channel_name}] Judder: {content[:200]} | {agent_name}: {response[:300]}"
            store_narrative(
                summary,
                metadata={
                    "type": "conversation",
                    "channel": channel_name,
                    "source": agent_id,
                    "agent_id": agent_id,
                },
            )
        except Exception:
            pass

        return _sanitize_response(response)

    async def _generate_response(self, content: str, channel_id: str, channel_name: str, is_dm: bool) -> str:
        """Generate an AI response to a user message."""
        from forven.ai import call_ai
        from forven.context import build_chat_context, store_conversation

        # Build lightweight chat context (no operational noise)
        context = build_chat_context()

        # Add conversation history for this channel
        history = self._history.get(channel_id, [])
        if history:
            context += "\n\n---\n\n# RECENT CONVERSATION IN THIS CHANNEL\n"
            for msg in history[-10:]:
                role = "Judder" if msg["role"] == "user" else "Forven"
                context += f"\n**{role}**: {msg['content'][:300]}\n"

        from forven.brain import resolve_brain_provider_model

        provider, model = resolve_brain_provider_model()

        # Add chat-mode instruction: no tools, use context data directly
        context += (
            "\n\n---\n\n# CHAT MODE\n"
            "You are in CHAT mode responding to Judder on Discord.\n"
            "You do NOT have tools, function calls, or API access. Do NOT generate XML, "
            "tool_call blocks, invoke tags, or any structured function calls.\n"
            "All the data you need is in the sections above — trades, portfolio, strategies, "
            "regime, memory. Use that data to answer directly in plain text.\n"
            "CRITICAL: Actionable requests are auto-queued by the Discord gateway when detected. "
            "If an action request reaches you without being queued, tell Judder to use `!brain <task>` or `!assign <agent> <task>`."
        )

        # Call AI
        response = await call_ai(
            provider=provider,
            model=model,
            prompt=content,
            system=context,
            max_tokens=4096,
            temperature=0.7,
        )

        # Update conversation history
        if channel_id not in self._history:
            self._history[channel_id] = []
        self._history[channel_id].append({"role": "user", "content": content})
        self._history[channel_id].append({"role": "assistant", "content": response})
        # Trim history
        if len(self._history[channel_id]) > self._history_limit * 2:
            self._history[channel_id] = self._history[channel_id][-self._history_limit * 2:]

        # Log to today's memory
        from forven.workspace import append_workspace, today_memory_path
        try:
            append_workspace(
                today_memory_path(),
                f"\n### Discord #{channel_name} — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
                f"**Judder**: {content[:200]}\n**Forven**: {response[:200]}\n",
            )
        except Exception as e:
            log.warning("Workspace memory append failed: %s", e)

        # AUTO-STORE: Save conversation to narrative memory for long-term recall
        try:
            await store_conversation(channel_name, content, response)
        except Exception as e:
            log.warning("Conversation store failed: %s", e)

        # Strip hallucinated tool calls before returning to Discord
        return _sanitize_response(response)

    async def _send_response(self, channel, response: str):
        """Send a response, splitting if needed for Discord's 2000 char limit."""
        if len(response) <= 2000:
            await channel.send(response)
        else:
            # Split on paragraph boundaries when possible
            chunks = []
            remaining = response
            while remaining:
                if len(remaining) <= 1990:
                    chunks.append(remaining)
                    break
                # Try to split at a paragraph break
                split_at = remaining.rfind("\n\n", 0, 1990)
                if split_at == -1:
                    split_at = remaining.rfind("\n", 0, 1990)
                if split_at == -1:
                    split_at = 1990
                chunks.append(remaining[:split_at])
                remaining = remaining[split_at:].lstrip("\n")

            for chunk in chunks:
                if chunk.strip():
                    await channel.send(chunk)

    async def wait_until_ready_custom(self, timeout: float = 30):
        """Wait for bot to be ready."""
        await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)

    # --- Background tasks ---

    @tasks.loop(seconds=30)
    async def scheduler_loop(self):
        """Run scheduler tick every 30 seconds."""
        try:
            from forven.scheduler import (
                _record_scheduler_tick_failure,
                _record_scheduler_tick_success,
                tick,
            )
            # ``tick()`` already has per-job timeouts, so avoid cancelling the
            # entire scheduler pass mid-queue.
            await tick()
            _record_scheduler_tick_success()
        except asyncio.CancelledError:
            log.warning("Scheduler tick cancelled (gateway reconnect?) — will resume on next iteration")
            # Do NOT re-raise: let the loop continue on the next iteration.
            # Re-raising CancelledError kills the tasks.loop permanently.
        except Exception as e:
            _record_scheduler_tick_failure(e)
            log.error("Scheduler tick error: %s", e, exc_info=True)

    @scheduler_loop.error
    async def scheduler_loop_error(self, error):
        """Auto-restart the scheduler loop if it crashes for any reason."""
        log.error("Scheduler loop crashed: %s — restarting in 10s", error, exc_info=error)
        await asyncio.sleep(10)
        if not self.scheduler_loop.is_running():
            self.scheduler_loop.restart()

    @scheduler_loop.before_loop
    async def before_scheduler(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=10)
    async def task_processor_loop(self):
        """Process pending brain tasks from the queue."""
        try:
            self._record_task_worker_heartbeat("brain")
            await self._process_pending_tasks()
        except asyncio.CancelledError:
            log.warning("Task processor cancelled — will resume on next iteration")
        except Exception as e:
            log.error("Task processor error: %s", e)

    @task_processor_loop.error
    async def task_processor_loop_error(self, error):
        """Auto-restart the task processor loop if it crashes."""
        log.error("Task processor loop crashed: %s — restarting in 10s", error, exc_info=error)
        await asyncio.sleep(10)
        if not self.task_processor_loop.is_running():
            self.task_processor_loop.restart()

    @task_processor_loop.before_loop
    async def before_task_processor(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=20)
    async def agent_runner_loop(self):
        """Process pending agent tasks for all enabled agents."""
        try:
            self._record_task_worker_heartbeat("agent")
            await self._process_agent_tasks()
        except asyncio.CancelledError:
            log.warning("Agent runner cancelled (gateway reconnect?) — will resume on next iteration")
            # Do NOT re-raise: re-raising CancelledError kills the tasks.loop
            # permanently. The loop must survive transient cancellations or the
            # whole agent task queue stops being processed (root cause of the
            # 2026-04-26 overnight stall — see git blame / PR for context).
        except Exception as e:
            log.error("Agent runner error: %s", e, exc_info=True)

    @agent_runner_loop.error
    async def agent_runner_loop_error(self, error):
        """Auto-restart the agent runner loop if it crashes for any reason."""
        log.error("Agent runner loop crashed: %s — restarting in 10s", error, exc_info=error)
        await asyncio.sleep(10)
        if not self.agent_runner_loop.is_running():
            self.agent_runner_loop.restart()

    @agent_runner_loop.before_loop
    async def before_agent_runner(self):
        await self.wait_until_ready()

    @tasks.loop(seconds=5)
    async def ops_queue_recovery_loop(self):
        """Consume bot-owned queue recovery requests from the control plane."""
        try:
            self._record_task_worker_heartbeat("ops")
            await self._consume_queue_processing_request()
        except Exception as e:
            log.error("Ops queue recovery error: %s", e)

    @ops_queue_recovery_loop.before_loop
    async def before_ops_queue_recovery(self):
        await self.wait_until_ready()

    async def _consume_queue_processing_request(self) -> None:
        raw_request = kv_get(QUEUE_PROCESS_REQUEST_KEY, {}) or {}
        if not isinstance(raw_request, dict):
            return

        request_id = str(raw_request.get("request_id") or "").strip()
        status = str(raw_request.get("status") or "").strip().lower()
        if not request_id or status not in {"queued", "processing"}:
            return

        if status == "processing" and is_active_request(raw_request):
            return

        requested_at = parse_timestamp(raw_request.get("updated_at") or raw_request.get("requested_at"))
        if requested_at is not None:
            age_seconds = (datetime.now(timezone.utc) - requested_at).total_seconds()
            if age_seconds > max(QUEUE_PROCESS_STALE_AFTER_SECONDS, 1.0):
                expired_result = build_queue_process_result(
                    request_id,
                    status="failed",
                    error="Queue processing request expired before the bot worker consumed it.",
                    worker_pid=os.getpid(),
                )
                kv_set(QUEUE_PROCESS_RESULT_KEY, expired_result)
                kv_set(
                    QUEUE_PROCESS_REQUEST_KEY,
                    {
                        "request_id": request_id,
                        "status": "failed",
                        "updated_at": utc_now_iso(),
                        "error": expired_result["error"],
                    },
                )
                return

        request_payload = dict(raw_request)
        request_payload["status"] = "processing"
        request_payload["worker_pid"] = os.getpid()
        request_payload["updated_at"] = utc_now_iso()
        kv_set(QUEUE_PROCESS_REQUEST_KEY, request_payload)

        processed_agent_tasks = False
        processed_brain_tasks = False
        error_message: str | None = None
        result_status = "completed"
        try:
            if bool(request_payload.get("process_agent_tasks")):
                await self._process_agent_tasks()
                processed_agent_tasks = True
            if bool(request_payload.get("process_brain_tasks")):
                await self._process_pending_tasks()
                processed_brain_tasks = True
        except Exception as exc:
            result_status = "failed"
            error_message = str(exc)
            log.exception("Queue processing request %s failed", request_id)

        result_payload = build_queue_process_result(
            request_id,
            status=result_status,
            agent_tasks_processed=processed_agent_tasks,
            brain_tasks_processed=processed_brain_tasks,
            error=error_message,
            worker_pid=os.getpid(),
        )
        kv_set(QUEUE_PROCESS_RESULT_KEY, result_payload)
        kv_set(
            QUEUE_PROCESS_REQUEST_KEY,
            {
                **request_payload,
                "status": result_status,
                "updated_at": utc_now_iso(),
                "error": error_message,
                "agent_tasks_processed": processed_agent_tasks,
                "brain_tasks_processed": processed_brain_tasks,
            },
        )

    async def _process_agent_tasks(self):
        """Run one round of task processing for all enabled agents concurrently."""
        from forven.db import claim_pending_agent_tasks, get_db

        await self._maybe_recover_stale_tasks("agent")

        with get_db() as conn:
            agents = conn.execute("SELECT * FROM agents WHERE enabled = 1").fetchall()
        from forven.agents.runner import run_agent_task

        coroutines = []

        async def _run_single_task(agent_dict, task_dict):
            channel = None
            target_channel = None
            task_id_raw = task_dict.get("id")
            try:
                task_id = int(task_id_raw)
            except Exception:
                task_id = 0
            if task_id:
                self._active_agent_task_ids.add(task_id)
            try:
                input_data = task_dict.get("input_data")
                if isinstance(input_data, str):
                    try:
                        input_data = json.loads(input_data)
                    except Exception:
                        input_data = {}
                if not isinstance(input_data, dict):
                    input_data = {}

                target_channel = input_data.get("_channel")

                result = await run_agent_task(agent_dict, dict(task_dict))

                if isinstance(result, dict) and result.get("error") and target_channel:
                    try:
                        channel_id = CHANNELS.get(str(target_channel), str(target_channel))
                        channel = self.get_channel(int(channel_id))
                        if not channel:
                            channel = await self.fetch_channel(int(channel_id))
                        if channel:
                            error_text = summarize_discord_text(result.get("error"), limit=280, max_lines=2) or str(result.get("error"))[:280]
                            await self._send_response(
                                channel,
                                f"Task failed: {task_dict.get('title', 'Untitled')}\nError: {error_text}",
                            )
                    except Exception as e:
                        log.warning("Could not post agent failure notification: %s", e)
            except Exception as e:
                log.error("Agent %s task %d error: %s", agent_dict["id"], task_dict["id"], e)
                if target_channel:
                    try:
                        channel_id = CHANNELS.get(str(target_channel), str(target_channel))
                        channel = self.get_channel(int(channel_id))
                        if not channel:
                            channel = await self.fetch_channel(int(channel_id))
                        if channel:
                            await self._send_response(
                                channel,
                                (
                                    f"Task failed: {task_dict.get('title', 'Untitled')}\n"
                                    f"Error: {summarize_discord_text(str(e), limit=280, max_lines=2) or str(e)[:280]}"
                                ),
                            )
                    except Exception as notify_err:
                        log.warning("Could not post agent failure notification: %s", notify_err)
            finally:
                if task_id:
                    self._active_agent_task_ids.discard(task_id)

        claimed_task_ids: set[int] = set()
        for agent in agents:
            agent = dict(agent)
            tasks = claim_pending_agent_tasks(agent["id"])
            for task in tasks:
                task_id_raw = task.get("id") if isinstance(task, dict) else None
                try:
                    task_id = int(task_id_raw)
                except Exception:
                    task_id = 0
                if task_id:
                    claimed_task_ids.add(task_id)
                    self._active_agent_task_ids.add(task_id)
                coroutines.append(_run_single_task(agent, task))

        if coroutines:
            # Run tasks concurrently, but limit total concurrent tasks to avoid slamming APIs/DBs
            from asyncio import Semaphore
            sem = Semaphore(3)  # limit concurrent API calls to avoid rate-limiting

            async def _sem_task(coro):
                async with sem:
                    await coro

            try:
                await asyncio.gather(*[_sem_task(c) for c in coroutines], return_exceptions=True)
            finally:
                for task_id in claimed_task_ids:
                    self._active_agent_task_ids.discard(task_id)

    async def _process_pending_tasks(self):
        """Pick up and execute pending brain tasks (from scheduler/queue).

        The Brain has full tool access — same as agents — so it can
        read/write files, run backtests, and update strategies.
        """
        await self._maybe_recover_stale_tasks("brain")

        from forven.db import get_db, claim_pending_tasks
        from forven.context import build_brain_context
        from forven.agents.runner import (
            _call_with_tools,
            AGENT_TOOLS,
            BRAIN_TOOLS,
            BACKTESTING_TOOLS,
            set_tool_context,
            reset_tool_context,
        )

        rows = claim_pending_tasks("brain_invoke", limit=None, priority=True)

        if not rows:
            # Idle: re-seed a brain cycle if the loop has stalled (a timed-out
            # callback can suppress its retry and leave nothing pending).
            try:
                from forven.runtime_worker import ensure_brain_keepalive

                ensure_brain_keepalive()
            except Exception:
                log.debug("brain keepalive (bot path) failed", exc_info=True)

        for task in rows:
            task = dict(task)
            task_id_raw = task.get("id")
            try:
                task_id = int(task_id_raw)
            except Exception:
                task_id = 0
            if task_id:
                self._active_brain_task_ids.add(task_id)
            raw_payload = task.get("payload", "{}")
            try:
                payload = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
            except Exception:
                payload = {}
            if not isinstance(payload, dict):
                payload = {}

            delivery_channel = payload.get("channel")

            try:
                message = payload.get("message", "Run your cycle.")
                source = str(payload.get("source") or "").strip()
                is_chat = source == "ui_chat"

                if source == "bootstrap":
                    from forven.brain import assign_research_cycle

                    assign_research_cycle()
                    response = (
                        "Bootstrap dispatched the strategy-developer research swarm "
                        "to create first-class hypotheses and initial strategy candidates."
                    )
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), json.dumps({"response": response}), task["id"]),
                        )
                    if delivery_channel:
                        try:
                            channel_id = CHANNELS.get(delivery_channel, delivery_channel)
                            channel = self.get_channel(int(channel_id))
                            if not channel:
                                channel = await self.fetch_channel(int(channel_id))
                            if channel:
                                outbound = _render_operational_discord_reply(
                                    response,
                                    source=source,
                                    task_message=message,
                                )
                                await self._send_response(channel, outbound)
                        except Exception as e:
                            log.warning("Could not post task result to channel %s: %s", delivery_channel, e)
                    log.info("Processed brain bootstrap task %d", task["id"])
                    continue

                from forven.brain import resolve_brain_provider_model

                provider, model = resolve_brain_provider_model(
                    payload.get("provider"),
                    payload.get("model"),
                )

                if is_chat:
                    # ── CHAT MODE: lightweight context, read-only tools, conversation history ──
                    from forven.context import build_chat_context
                    context = build_chat_context()

                    # Build multi-turn messages from conversation history
                    history = payload.get("history") or []
                    messages = []
                    for entry in history[-20:]:
                        role = entry.get("role", "")
                        content = str(entry.get("content", "")).strip()
                        if role in ("user", "assistant") and content:
                            messages.append({"role": role, "content": content})
                    messages.append({"role": "user", "content": message})

                    # Full-featured tools for chat — can look things up AND take actions
                    _chat_tool_names = {
                        # Read / search
                        "read_file", "search_memory", "search_chroma",
                        # Action tools
                        "run_backtest", "run_shell", "write_file",
                        # Brain tools
                        "assign_agent_task", "promote_strategy", "create_strategy",
                        # Backtesting tools
                        "forven_run_backtest", "forven_list_datasets", "forven_get_results",
                    }
                    chat_tools = [t for t in AGENT_TOOLS + BRAIN_TOOLS + BACKTESTING_TOOLS if t["name"] in _chat_tool_names]
                    if chat_tools:
                        context += (
                            "\n\n---\n\n# TOOLS\n"
                            "You have tools to look things up AND take actions when asked:\n"
                            "- **read_file**: Read workspace files\n"
                            "- **search_memory**: Search long-term memory\n"
                            "- **search_chroma**: Search past experiments and post-mortems\n"
                            "- **run_backtest**: Run a backtest for a specific Strategy Container\n"
                            "- **run_shell**: Execute shell commands\n"
                            "- **write_file**: Create or update files\n"
                            "- **assign_agent_task**: Assign a task to an agent\n"
                            "- **promote_strategy**: Promote a specific Strategy Container to the next lifecycle stage\n"
                            "- **create_strategy**: Create a new Strategy Container (S0000X)\n"
                            "- **forven_run_backtest**: Run a backtest via the Forven pipeline\n"
                            "- **forven_list_datasets**: List available datasets\n"
                            "- **forven_get_results**: Retrieve backtest results\n\n"
                            "Use these tools proactively when the user asks you to do something. "
                            "Don't just describe what could be done — actually do it."
                        )

                    brain_context_tokens = set_tool_context("brain", f"B{int(task['id']):04d}")
                    try:
                        response = await asyncio.wait_for(
                            _call_with_tools(provider, model, messages, context, tools=chat_tools or None),
                            timeout=_BRAIN_TASK_TIMEOUT_SECONDS,
                        )
                    finally:
                        reset_tool_context(brain_context_tokens)

                    with get_db() as conn:
                        conn.execute(
                            "UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), json.dumps({"response": response[:2000]}), task["id"]),
                        )

                else:
                    # ── OPERATIONAL MODE: full context, all tools, agent tasks, post-mortems ──
                    ui_context = str(payload.get("context") or "").strip()
                    if ui_context:
                        message = f"[UI Context: {ui_context}]\n\n{message}"

                    context = build_brain_context("main")

                    # Add tool instructions — Brain gets all base tools + brain-only tools
                    context += (
                        "\n\n---\n\n# TOOLS\n"
                        "You have tools available. USE THEM — don't just describe what you'd do:\n\n"
                        "## Agent Management (Brain-only)\n"
                        "- **assign_agent_task**: Assign a task to one of your 7 agents. BE SPECIFIC in the description.\n"
                        "- **promote_strategy**: Change Strategy Container lifecycle status (Ideation -> Test -> Paper -> Live -> Archived)\n"
                        "- **create_strategy**: Create a new Strategy Container record (S0000X) in the database\n\n"
                        "## Research & Analysis\n"
                        "- **run_shell**: Execute commands (backtests, data checks)\n"
                        "- **read_file**: Read workspace files (LESSONS.md, evolution_journal.md, memory/)\n"
                        "- **write_file**: Write/append to workspace files (update lessons, log findings, evolve strategies)\n"
                        "- **search_memory**: Search narrative memory for long-term context\n"
                        "- **store_memory**: Store insights in narrative memory\n"
                        "- **run_backtest**: Run a strategy backtest and get fitness score\n"
                        "- **search_chroma**: Search ChromaDB for past experiments, post-mortems, and execution slippage\n"
                        "- **store_chroma**: Store data in ChromaDB vector store\n\n"
                        "## Backtesting (Forven)\n"
                        "- **forven_list_datasets**: List available backtesting datasets\n"
                        "- **forven_create_strategy**: Create a strategy on Forven Backtesting\n"
                        "- **forven_run_backtest**: Run a backtest with realistic fees\n"
                        "- **forven_run_optimization**: Run parameter optimization\n"
                        "- **forven_run_verdict**: Validate a strategy\n"
                        "- **forven_get_results**: Get detailed backtest results\n\n"
                        "IMPORTANT: When you review agent output, ALWAYS assign follow-up tasks.\n"
                        "When you discover a lesson, UPDATE LESSONS.md. When you evolve a strategy, UPDATE evolution_journal.md.\n"
                        "When you find something worth remembering long-term, STORE it in narrative memory.\n"
                        "You are the Brain — you don't just observe, you ACT. Assign work NOW."
                    )

                    # Fetch completed agent tasks to review
                    from forven.brain import (
                        _get_completed_agent_tasks, mark_agent_tasks_reviewed,
                        _get_pending_post_mortems, _clear_post_mortems,
                    )

                    completed_tasks = _get_completed_agent_tasks()
                    task_ids_to_mark = []
                    if completed_tasks:
                        context += "\n\n---\n\n# COMPLETED AGENT TASKS (awaiting your review)\n"
                        context += "Review these, update strategies/lessons, and assign new tasks if necessary.\n"
                        for t in completed_tasks:
                            task_ids_to_mark.append(t["id"])
                            context += f"\n## [{t['agent_id']}] {t.get('title', 'Untitled')}\n"
                            if t.get("output_data"):
                                try:
                                    out = json.loads(t["output_data"]) if isinstance(t["output_data"], str) else t["output_data"]
                                    context += f"Output:\n```\n{json.dumps(out, indent=2)[:3000]}\n```\n"
                                except Exception:
                                    context += f"Output: {t['output_data'][:3000]}\n"

                    # Include pending trade post-mortems
                    post_mortems = _get_pending_post_mortems()
                    if post_mortems:
                        context += "\n\n---\n\n# PENDING TRADE POST-MORTEMS\n"
                        context += (
                            "These trades were recently closed. Analyze each one:\n"
                            "- What worked? What failed? Why?\n"
                            "- Update LESSONS.md with insights\n"
                            "- Assign follow-up tasks to agents if needed\n"
                        )
                        for pm in post_mortems:
                            context += (
                                f"\n## Trade {pm.get('trade_id', '?')} — {pm.get('strategy', '?')}\n"
                                f"Direction: {pm.get('direction', '?')} | PnL: {pm.get('pnl_pct', 0):+.2%}\n"
                                f"Entry: ${pm.get('entry_price', 0):,.2f} → Exit: ${pm.get('exit_price', 0):,.2f}\n"
                                f"Reason: {pm.get('reason', '?')} | Closed: {pm.get('closed_at', '?')}\n"
                            )

                    # Call AI WITH tool access — Brain gets all tools including brain-only.
                    # Use per-task tool context so Brain tool calls are permissioned correctly.
                    brain_tools = AGENT_TOOLS + BRAIN_TOOLS + BACKTESTING_TOOLS
                    messages = [{"role": "user", "content": message}]
                    brain_context_tokens = set_tool_context("brain", f"B{int(task['id']):04d}")
                    try:
                        response = await asyncio.wait_for(
                            _call_with_tools(provider, model, messages, context, tools=brain_tools),
                            timeout=_BRAIN_TASK_TIMEOUT_SECONDS,
                        )
                    finally:
                        reset_tool_context(brain_context_tokens)

                    if task_ids_to_mark:
                        mark_agent_tasks_reviewed(task_ids_to_mark)
                    if post_mortems:
                        _clear_post_mortems()
                        log.info("Brain processed %d trade post-mortems", len(post_mortems))

                    with get_db() as conn:
                        conn.execute(
                            "UPDATE tasks SET status='done', completed_at=?, result=? WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), json.dumps({"response": response[:2000]}), task["id"]),
                        )

                    # Store brain cycle narrative in ChromaDB
                    try:
                        from forven.vectordb import store_narrative
                        store_narrative(
                            f"[Brain] {response[:500]}",
                            metadata={"type": "brain_cycle", "source": "forven"},
                        )
                    except Exception:
                        pass

                # Post response to Discord if channel specified
                delivery_channel = payload.get("channel")
                if delivery_channel:
                    try:
                        # Resolve string channel names to IDs
                        channel_id = CHANNELS.get(delivery_channel, delivery_channel)
                        channel = self.get_channel(int(channel_id))
                        if not channel:
                            channel = await self.fetch_channel(int(channel_id))
                        if channel:
                            sanitized = _sanitize_response(response)
                            outbound = sanitized if is_chat else _render_operational_discord_reply(
                                sanitized,
                                source=source,
                                task_message=message,
                            )
                            await self._send_response(channel, outbound)
                    except Exception as e:
                        log.warning("Could not post task result to channel %s: %s", delivery_channel, e)

                log.info("Processed brain task %d", task["id"])

            except Exception as e:
                if isinstance(e, asyncio.TimeoutError):
                    error_detail = f"Brain task timeout after {_BRAIN_TASK_TIMEOUT_SECONDS:.2f}s"
                else:
                    error_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                log.error("Brain task %d failed: %s", task["id"], error_detail, exc_info=True)
                if isinstance(e, asyncio.TimeoutError):
                    from forven.db import requeue_brain_task

                    requeue_brain_task(
                        int(task["id"]),
                        error_detail,
                        backoff_seconds=_BRAIN_TRANSIENT_BACKOFF_SECONDS,
                        max_retries=_MAX_BRAIN_PROVIDER_RETRIES,
                        exhausted_label="Brain task timeout retries exhausted",
                    )
                elif _is_rate_limit_exception(e):
                    from forven.db import requeue_brain_task

                    requeue_brain_task(
                        int(task["id"]),
                        f"Rate-limited by provider: {error_detail[:350]}",
                        backoff_seconds=_BRAIN_RATE_LIMIT_BACKOFF_SECONDS,
                        max_retries=_MAX_BRAIN_PROVIDER_RETRIES,
                        exhausted_label="Rate-limit retries exhausted",
                    )
                elif is_transient_provider_exception(e):
                    from forven.db import requeue_brain_task

                    requeue_brain_task(
                        int(task["id"]),
                        f"Provider unavailable; requeued for retry: {error_detail[:350]}",
                        backoff_seconds=_BRAIN_TRANSIENT_BACKOFF_SECONDS,
                        max_retries=_MAX_BRAIN_PROVIDER_RETRIES,
                        exhausted_label="Provider retries exhausted",
                    )
                else:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE tasks SET status='failed', error=?, completed_at=? WHERE id=?",
                            (error_detail[:500], datetime.now(timezone.utc).isoformat(), task["id"]),
                        )
                continue
            finally:
                if task_id:
                    self._active_brain_task_ids.discard(task_id)


# Singleton bot instance
_bot: ForvenBot | None = None


def get_bot() -> ForvenBot:
    """Get or create the singleton bot instance."""
    global _bot
    if _bot is None:
        _bot = ForvenBot(agent_id=None)
    return _bot


async def send(channel_name: str, message: str, channel_id: str | None = None, agent_id: str | None = None):
    """Send a message to a named channel or specific channel ID."""
    target_id = channel_id or CHANNELS.get(channel_name)
    if not target_id:
        raise ValueError(f"Unknown channel: {channel_name}. Available: {list(CHANNELS.keys())}")

    async def _send_with_bot(selected_bot: ForvenBot):
        await selected_bot.wait_until_ready_custom()
        channel = selected_bot.get_channel(int(target_id))
        if not channel:
            channel = await selected_bot.fetch_channel(int(target_id))
        if not channel:
            raise RuntimeError(f"Could not find channel: {target_id}")
        await selected_bot._send_response(channel, message)

    main_bot = get_bot()
    await _send_with_bot(main_bot)
    log.info("Sent message to %s (%s): %s...", channel_name or target_id, target_id, message[:50])


async def send_thread(channel_name: str, title: str, message: str, channel_id: str | None = None, embeds: list[dict] | None = None, agent_id: str | None = None):
    """Send a message into a newly created thread in a named channel."""
    target_id = channel_id or CHANNELS.get(channel_name)
    if not target_id:
        raise ValueError(f"Unknown channel: {channel_name}")

    # Discord library uses discord.Embed objects
    discord_embeds = []
    if embeds:
        for e in embeds:
            de = discord.Embed(
                title=e.get("title"),
                description=e.get("description"),
                color=e.get("color", discord.Color.default())
            )
            discord_embeds.append(de)

    async def _send_thread_with_bot(selected_bot: ForvenBot):
        await selected_bot.wait_until_ready_custom()
        channel = selected_bot.get_channel(int(target_id))
        if not channel:
            channel = await selected_bot.fetch_channel(int(target_id))

        if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
            raise TypeError(f"Channel {target_id} does not support threading")

        thread = await channel.create_thread(name=title, type=discord.ChannelType.public_thread)
        if len(message) > 2000:
            await selected_bot._send_response(thread, message)
            if discord_embeds:
                for i in range(0, len(discord_embeds), 10):
                    await thread.send(embeds=discord_embeds[i:i+10])
        else:
            await thread.send(content=message, embeds=discord_embeds[:10] if discord_embeds else None)
            if discord_embeds and len(discord_embeds) > 10:
                for i in range(10, len(discord_embeds), 10):
                    await thread.send(embeds=discord_embeds[i:i+10])

    await _send_thread_with_bot(get_bot())
    log.info("Created thread '%s' in %s and sent message/embeds", title, channel_name or target_id)


# Per-channel circuit-breaker for a persistent Discord 403 ("Missing Access").
# A forbidden channel is an operator-side permission gap, not a transient error:
# without a breaker, every notification re-POSTs -> 403 -> warning -> raise, which
# produced ~200 paired warnings overnight. We back off per channel for an hour and
# log ONCE; a later 200/201 (access re-granted) auto-clears the breaker.
_DISCORD_FORBIDDEN_COOLDOWN_SECONDS = 3600
_DISCORD_FORBIDDEN_CHANNELS: dict[str, float] = {}


def _discord_channel_circuit_open(channel_id) -> bool:
    expiry = _DISCORD_FORBIDDEN_CHANNELS.get(str(channel_id))
    if expiry is None:
        return False
    if time.time() >= expiry:
        _DISCORD_FORBIDDEN_CHANNELS.pop(str(channel_id), None)
        return False
    return True


def _trip_discord_channel_circuit(channel_id) -> None:
    key = str(channel_id)
    already_open = _discord_channel_circuit_open(key)
    _DISCORD_FORBIDDEN_CHANNELS[key] = time.time() + _DISCORD_FORBIDDEN_COOLDOWN_SECONDS
    if not already_open:
        log.warning(
            "Discord bot lacks access to channel %s (403 Missing Access) — grant the bot "
            "channel access or disable this notification sink; suppressing further sends for %dm",
            key,
            _DISCORD_FORBIDDEN_COOLDOWN_SECONDS // 60,
        )


def _clear_discord_channel_circuit(channel_id) -> None:
    _DISCORD_FORBIDDEN_CHANNELS.pop(str(channel_id), None)


def send_thread_sync(channel_name: str, title: str, message: str, channel_id: str | None = None) -> bool:
    """Synchronous send to a new thread via Discord REST API."""
    import httpx

    target_id = channel_id or CHANNELS.get(channel_name)
    if not target_id:
        raise ValueError(f"Unknown channel: {channel_name}")
    if _discord_channel_circuit_open(target_id):
        return False

    token = get_bot_token()
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}

    # 1. Create thread
    thread_resp = httpx.post(
        f"https://discord.com/api/v10/channels/{target_id}/threads",
        headers=headers,
        json={"name": title, "type": 11}, # 11 = public_thread
        timeout=10,
    )
    if thread_resp.status_code == 403:
        _trip_discord_channel_circuit(target_id)
        return False
    if thread_resp.status_code not in (200, 201):
        detail = _discord_response_detail(thread_resp) or f"HTTP {thread_resp.status_code}"
        log.warning("Discord REST thread creation failed (%d): %s", thread_resp.status_code, detail)
        raise RuntimeError(f"Discord thread creation failed: {detail}")

    try:
        thread_payload = thread_resp.json()
    except Exception as exc:
        raise RuntimeError(f"Discord thread creation returned invalid JSON: {exc}") from exc
    thread_id = str(thread_payload.get("id") or "").strip()
    if not thread_id:
        raise RuntimeError("Discord thread creation returned no thread id")

    # 2. Post message to thread
    if len(message) > 1900:
        message = message[:1900] + "\n..."

    msg_resp = httpx.post(
        f"https://discord.com/api/v10/channels/{thread_id}/messages",
        headers=headers,
        json={"content": message},
        timeout=10,
    )
    if msg_resp.status_code == 403:
        _trip_discord_channel_circuit(target_id)
        return False
    if msg_resp.status_code not in (200, 201):
        detail = _discord_response_detail(msg_resp) or f"HTTP {msg_resp.status_code}"
        log.warning("Discord REST thread message failed (%d): %s", msg_resp.status_code, detail)
        raise RuntimeError(f"Discord thread message failed: {detail}")
    _clear_discord_channel_circuit(target_id)
    return True


def send_sync(channel_name: str, message: str, channel_id: str | None = None) -> bool:
    """Synchronous send via Discord REST API — works from any process."""
    import httpx

    target_id = channel_id or CHANNELS.get(channel_name)
    if not target_id:
        raise ValueError(f"Unknown channel: {channel_name}")
    if _discord_channel_circuit_open(target_id):
        return False

    token = get_bot_token()
    # Truncate to Discord's 2000 char limit
    if len(message) > 1900:
        message = message[:1900] + "\n..."

    resp = httpx.post(
        f"https://discord.com/api/v10/channels/{target_id}/messages",
        headers={"Authorization": f"Bot {token}", "Content-Type": "application/json"},
        json={"content": message},
        timeout=10,
    )
    if resp.status_code == 403:
        _trip_discord_channel_circuit(target_id)
        return False
    if resp.status_code not in (200, 201):
        detail = _discord_response_detail(resp) or f"HTTP {resp.status_code}"
        log.warning("Discord REST send failed (%d): %s", resp.status_code, detail)
        raise RuntimeError(f"Discord send failed: {detail}")
    _clear_discord_channel_circuit(target_id)
    return True


def _discord_response_detail(resp) -> str:
    """Extract a concise error detail from a Discord HTTP response."""
    try:
        payload = resp.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("message")
        if detail:
            return str(detail)
    raw = (resp.text or "").strip()
    return raw[:240]


def run_discord_audit(send_probe: bool = False) -> dict:
    """Audit gateway bot access to all configured operational Discord channels."""
    import httpx
    from concurrent.futures import ThreadPoolExecutor
    from forven.reporter import AGENT_CHANNEL_MAP
    request_timeout_seconds = 2.0

    def _check_token_channel(
        actor: str,
        token: str,
        channel_alias: str,
        channel_id: str | None,
        *,
        probe_send: bool,
    ) -> dict:
        record = {
            "actor": actor,
            "channel_alias": channel_alias,
            "channel_id": str(channel_id or ""),
            "can_view": False,
            "can_send": None,
            "view_status": None,
            "send_status": None,
            "status": "error",
            "detail": "",
        }
        if not token:
            record["status"] = "missing_token"
            record["detail"] = "token not configured"
            return record
        if not channel_id:
            record["status"] = "missing_channel"
            record["detail"] = "channel alias is not mapped"
            return record

        headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
        try:
            with httpx.Client(timeout=request_timeout_seconds) as client:
                view_resp = client.get(f"https://discord.com/api/v10/channels/{channel_id}", headers=headers)
                record["view_status"] = int(view_resp.status_code)
                if view_resp.status_code == 200:
                    record["can_view"] = True
                    if probe_send:
                        probe_msg = (
                            f"[Forven Discord audit] {actor} probe at "
                            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
                        )
                        send_resp = client.post(
                            f"https://discord.com/api/v10/channels/{channel_id}/messages",
                            headers=headers,
                            json={"content": probe_msg[:1900]},
                        )
                        record["send_status"] = int(send_resp.status_code)
                        record["can_send"] = send_resp.status_code in (200, 201)
                        if record["can_send"]:
                            record["status"] = "ok"
                        else:
                            record["status"] = "send_failed"
                            record["detail"] = _discord_response_detail(send_resp)
                    else:
                        record["status"] = "ok"
                else:
                    record["status"] = "view_failed"
                    record["detail"] = _discord_response_detail(view_resp)
        except Exception as exc:
            record["status"] = "error"
            record["detail"] = str(exc)[:240]

        return record

    results: list[dict] = []
    queued_checks: list[dict] = []
    config = load_config()
    owner_val = config.get("discord_owner_id")
    owner_id = int(owner_val) if owner_val else 0

    main_token = ""
    try:
        main_token = get_bot_token().strip()
    except Exception:
        main_token = ""

    channel_aliases: list[str] = []
    seen_aliases: set[str] = set()
    for alias in ("general", "heartbeat", "alerts"):
        if alias not in seen_aliases:
            channel_aliases.append(alias)
            seen_aliases.add(alias)
    for alias in sorted({str(value).strip() for value in AGENT_CHANNEL_MAP.values() if str(value).strip()}):
        if alias not in seen_aliases:
            channel_aliases.append(alias)
            seen_aliases.add(alias)

    for alias in channel_aliases:
        queued_checks.append(
            {
                "actor": "gateway",
                "token": main_token,
                "channel_alias": alias,
                "channel_id": CHANNELS.get(alias),
                "probe_send": send_probe,
            }
        )

    if queued_checks:
        max_workers = min(8, len(queued_checks))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(
                    _check_token_channel,
                    spec["actor"],
                    spec["token"],
                    spec["channel_alias"],
                    spec["channel_id"],
                    probe_send=bool(spec.get("probe_send", False)),
                )
                for spec in queued_checks
            ]
            for spec, future in zip(queued_checks, futures):
                try:
                    record = future.result()
                except Exception as exc:
                    record = {
                        "actor": spec.get("actor"),
                        "channel_alias": spec.get("channel_alias"),
                        "channel_id": str(spec.get("channel_id") or ""),
                        "can_view": False,
                        "can_send": None,
                        "view_status": None,
                        "send_status": None,
                        "status": "error",
                        "detail": str(exc)[:240],
                    }
                results.append(record)

    failures = [
        r
        for r in results
        if r.get("status") != "ok"
    ]
    return {
        "status": "ok" if not failures else "degraded",
        "owner_guard_enabled": owner_id > 0,
        "owner_id_configured": owner_id,
        "send_probe": bool(send_probe),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
        "summary": {
            "total": len(results),
            "ok": len(results) - len(failures),
            "failed": len(failures),
            "failures": [
                {
                    "actor": f.get("actor"),
                    "channel_alias": f.get("channel_alias"),
                    "status": f.get("status"),
                    "detail": f.get("detail"),
                }
                for f in failures
            ],
        },
    }


async def start_bot():
    """Start the Discord bot (blocking)."""
    os.environ.setdefault("FORVEN_DISABLE_CHROMA_IN_PROCESS", "1")
    if not _acquire_bot_lock():
        status = get_bot_lock_status()
        active_pid = status.get("active_pid")
        pid_suffix = f" (pid {active_pid})" if active_pid else ""
        log.warning("Another bot instance is already running%s; skipping duplicate start.", pid_suffix)
        return

    try:
        log.info("Bot process forcing subprocess-only/disabled ChromaDB access for stability on this host")
        # Symmetry with run_bot(): arm fail-closed spend enforcement before any
        # owns-runtime loop can start.
        from forven.model_selection import ensure_enforcement_armed
        ensure_enforcement_armed()
        await _run_all_bots()
    finally:
        _release_bot_lock()



async def _run_agent_bot_safe(agent_id: str, token: str, delay: float = 0):
    """Deprecated multi-bot entrypoint retained as a no-op."""
    log.info("Ignoring legacy per-agent bot start for %s; gateway bot handles Discord delivery.", agent_id)


async def _run_all_bots():
    token = get_bot_token()
    main_bot = get_bot()
    await main_bot.start(token)


async def start_agent_bot(agent_id: str, token: str):
    """Deprecated multi-bot runtime hook retained as a no-op."""
    log.info("Ignoring legacy agent bot restart for %s; gateway bot handles Discord delivery.", agent_id)


async def _sync_agent_bots():
    """Deprecated multi-bot sync hook retained as a no-op."""
    return None


def reload_agent_bot(agent_id: str):
    """Legacy compatibility hook for the retired multi-bot runtime."""
    log.info("reload_agent_bot called for %s — single gateway bot mode ignores per-agent Discord tokens", agent_id)

def run_bot():
    """Run the Discord bot — gateway + scheduler + task processor."""
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    os.environ.setdefault("FORVEN_DISABLE_CHROMA_IN_PROCESS", "1")
    if not _acquire_bot_lock():
        status = get_bot_lock_status()
        active_pid = status.get("active_pid")
        pid_suffix = f" (pid {active_pid})" if active_pid else ""
        log.warning("Another bot instance is already running%s; skipping duplicate start.", pid_suffix)
        try:
            from forven.db import init_db, log_activity

            init_db()
            log_activity(
                "info",
                "bot",
                "Skipped bot start (singleton lock already held)",
                {"active_pid": active_pid},
            )
        except Exception:
            pass
        return

    try:
        log.info("Bot process forcing subprocess-only/disabled ChromaDB access for stability on this host")
        log.info("Starting Forven gateway (bot + scheduler + task processor)")
        # Arm fail-closed spend enforcement BEFORE any task/scheduler loop starts,
        # so a bot-owns-runtime process never spends on an unconnected/unselected
        # (provider, model) just because the API lifespan hasn't run on this DB.
        from forven.model_selection import ensure_enforcement_armed
        ensure_enforcement_armed()
        try:
            asyncio.run(_run_all_bots())
        except discord.errors.LoginFailure as exc:
            log.critical(
                "Discord rejected the bot token (LoginFailure: %s). "
                "Rotate the token in the Discord Developer Portal and update "
                "%s (key 'discord_token') or the DISCORD_TOKEN env var, then restart. "
                "Exiting with code 78 so the start_all watchdog stops restart-looping.",
                exc,
                FORVEN_HOME / "config.json",
            )
            import sys as _sys
            _sys.exit(78)
        except BaseException as exc:
            log.critical(
                "Gateway bot exited: %s: %s",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            raise
    finally:
        _release_bot_lock()
