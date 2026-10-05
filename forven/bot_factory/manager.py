"""Bot process manager — spawn, stop, monitor, and recover bot subprocesses."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path

import psutil

from forven.bot_factory.broker_client import BOT_TOKEN_ENV, BROKER_HOST_ENV, NO_MASTER_KEY_ENV
from forven.bot_factory.os_isolation import (
    SandboxStatus,
    ensure_sandbox_ready,
    sandbox_enabled,
    sandbox_env,
    spawn_sandboxed,
)
from forven.config import FORVEN_DB, FORVEN_HOME
from forven.db import (
    get_bot,
    get_running_bots,
    log_activity,
    reconcile_orphaned_bot_trades,
    set_bot_status,
)

logger = logging.getLogger(__name__)

# How often to check heartbeats (seconds)
_MONITOR_INTERVAL = 30
# How long before a heartbeat is considered stale
# Must be generous — LLM calls + market data fetch can take 30-60s per tick
_HEARTBEAT_STALE_SECONDS = 180
_REPO_ROOT = Path(__file__).resolve().parents[2]


# ── Per-bot credential broker (parent side) ─────────────────────────
#
# Bot subprocesses never receive the master encryption key, so they cannot
# decrypt Forven's secret stores (provider logins, exchange keys, webhooks).
# Each spawn is issued a random token; the bot presents it to the parent API,
# which decrypts and returns ONLY the LLM provider credential that bot is
# configured to use. OAuth refresh happens here, in the process that owns the
# encrypted store. The bot-side client is forven.bot_factory.broker_client.

_KV_PREFIX = "forven:bot-cred-token:"


def _kv_key(bot_id: str) -> str:
    return f"{_KV_PREFIX}{bot_id}"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_bot_token(bot_id: str, provider: str) -> str:
    """Mint a fresh token for one bot spawn; only its hash is stored.

    The hash is persisted (not held in memory) so a bot that survives an API
    restart and is adopted by the new process can still fetch credentials.
    The provider is pinned to the spawn: the runner loads its config once, so
    an edit to a running bot's model must not switch which login it is served.
    """
    from forven.db import kv_set

    token = secrets.token_urlsafe(32)
    kv_set(_kv_key(bot_id), {"hash": _digest(token), "provider": provider})
    return token


def verify_bot_token(bot_id: str, token: str | None) -> str | None:
    """The provider pinned to this bot's spawn when the token matches, else None."""
    from forven.db import kv_get

    if not bot_id or not token:
        return None
    record = kv_get(_kv_key(bot_id))
    if not isinstance(record, dict):
        return None
    expected = str(record.get("hash") or "")
    provider = str(record.get("provider") or "")
    if not expected or not provider or not hmac.compare_digest(expected, _digest(token)):
        return None
    return provider


def revoke_bot_token(bot_id: str) -> None:
    from forven.db import kv_set

    try:
        kv_set(_kv_key(bot_id), {})
    except Exception:
        logger.debug("Could not revoke credential token for bot %s", bot_id, exc_info=True)


def bot_process_is_live(bot_id: str) -> bool:
    """True only while the bot is recorded as running on a live PID.

    The broker requires this on top of the token, so a token copied out of a
    bot that has since exited (on any path, including a runner-initiated
    shutdown that never reaches monitor_bots) stops working.
    """
    from forven.db import get_bot_status

    status = get_bot_status(bot_id) or {}
    pid = status.get("pid")
    return status.get("status") == "running" and bool(pid) and _is_pid_alive(int(pid))


def resolve_bot_provider(bot_config: dict) -> str:
    """The LLM provider a bot's model resolves to (canonical resolver)."""
    try:
        from forven.ai import normalize_provider_and_model

        provider, _ = normalize_provider_and_model("auto", bot_config.get("model") or "")
        return str(provider or "openai")
    except Exception:
        return "openai"


def brokered_credential_for_bot(bot_id: str, provider: str) -> dict:
    """Decrypt and return only the bot's own provider credential.

    ``provider`` is the one pinned to the bot's spawn token, never taken from
    the request, so a bot cannot ask for another provider's login. Raises
    LookupError when the bot is unknown and ValueError when the provider has
    no usable credential.
    """
    from forven.auth.store import get_profile, get_token
    from forven.db import get_bot

    if not get_bot(bot_id):
        raise LookupError(f"Bot {bot_id} not found")
    try:
        token = get_token(provider)  # refreshes OAuth in the parent if needed
    except Exception as exc:
        raise ValueError(f"No usable {provider} credential: {exc}") from exc
    profile = get_profile(provider) or {}
    payload: dict = {"provider": provider, "access": token}
    base_url = str(profile.get("base_url") or "").strip()
    if base_url:
        payload["base_url"] = base_url
    expires = profile.get("expires")
    if isinstance(expires, (int, float)) and expires > 0:
        payload["expires"] = int(expires)
    return payload


def _is_pid_alive(pid: int) -> bool:
    """Check if a process is alive by PID."""
    if pid <= 0:
        return False
    try:
        process = psutil.Process(pid)
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    try:
        if not process.is_running():
            return False
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied:
        return True
    return True


def _build_isolated_env(bot_config: dict, bot_token: str | None = None) -> dict[str, str]:
    """Build a minimal environment for the bot subprocess.

    Passes FORVEN_HOME, BOT_ID, minimal system vars, the ChromaDB in-process
    guard, and only the credential(s) for the bot's RESOLVED LLM provider. Does
    NOT inherit the parent's full environment (no Discord/exchange secrets, etc.).
    """
    env = {
        "FORVEN_HOME": str(FORVEN_HOME),
        "BOT_ID": bot_config["id"],
        # Minimal system env needed for Python to function
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        # Windows needs these for Path.home() and user-level operations
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "HOMEDRIVE": os.environ.get("HOMEDRIVE", ""),
        "HOMEPATH": os.environ.get("HOMEPATH", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
    }

    # ISO-4: the subprocess does NOT inherit the parent's os.environ, so it
    # would miss the in-process ChromaDB segfault guard for BotMemory's own
    # chroma store (FORVEN_HOME/chroma/bots — separate from the removed main
    # vector layer). The API process no longer sets the guard globally, so
    # default it ON for spawned bots on Windows, where the ONNX crash lives;
    # an operator can export FORVEN_DISABLE_CHROMA_IN_PROCESS=0 to override.
    if sys.platform.startswith("win"):
        env.setdefault("FORVEN_DISABLE_CHROMA_IN_PROCESS", "1")
    # #117: the bot never gets the master encryption key, whether it comes
    # from FORVEN_ENCRYPTION_KEY or the key file, and its secret_storage
    # refuses to load one. Its own provider login is served by the parent's
    # credential broker (below) using this per-spawn token.
    env[NO_MASTER_KEY_ENV] = "1"
    if bot_token:
        env[BOT_TOKEN_ENV] = bot_token
    env["FORVEN_PORT"] = os.environ.get("FORVEN_PORT", "8003")
    env[BROKER_HOST_ENV] = _broker_host()

    for guard_var in (
        "FORVEN_DISABLE_CHROMA_IN_PROCESS",
        "FORVEN_DISABLE_CHROMA",
        "ANONYMIZED_TELEMETRY",
    ):
        val = os.environ.get(guard_var)
        if val:
            env[guard_var] = val

    # Forward only the credential(s) for the bot's RESOLVED provider, derived
    # via the canonical resolver (not a model-name substring heuristic), so a
    # zai / openrouter / anthropic / deepseek bot whose key lives only in the
    # environment can actually authenticate.
    provider = resolve_bot_provider(bot_config)

    try:
        from forven.auth.store import _ENV_ACCESS_TOKEN_KEYS, _ENV_BASE_URL_KEYS

        token_keys = _ENV_ACCESS_TOKEN_KEYS.get(provider, ("OPENAI_API_KEY",))
        base_url_keys = _ENV_BASE_URL_KEYS.get(provider, ())
    except Exception:
        token_keys = ("OPENAI_API_KEY",)
        base_url_keys = ()

    forwarded = False
    for var in (*token_keys, *base_url_keys):
        val = os.environ.get(var)
        if val:
            env[var] = val
            forwarded = True

    # Safety net: keep the prior "unknown → OpenAI" behavior so a provider whose
    # credential lives on-disk (FORVEN_HOME, already forwarded) still has a key
    # path, and a misresolved provider isn't left with nothing.
    if not forwarded:
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            env["OPENAI_API_KEY"] = openai_key

    # A LIVE-armed bot places real exchange orders from its subprocess through
    # the sanctioned live stack (bot_factory.live_exec). Hyperliquid credentials
    # normally come from the encrypted KV settings store (reachable via
    # FORVEN_HOME, already forwarded); when the operator supplies them via
    # environment instead, forward exactly those vars — and ONLY for live bots,
    # keeping paper bots exchange-credential-free.
    if str(bot_config.get("execution_mode") or "paper").strip().lower() == "live":
        for var in (
            "FORVEN_HL_API_SECRET", "HL_API_SECRET",
            "FORVEN_HL_API_KEY", "HL_API_KEY",
            "FORVEN_HL_WALLET_ADDRESS", "HL_WALLET_ADDRESS",
            "FORVEN_HL_USE_TESTNET", "USE_TESTNET",
        ):
            val = os.environ.get(var)
            if val:
                env[var] = val
        # Without the master key the bot can't decrypt the Settings store
        # itself, so the parent resolves the Hyperliquid credentials and
        # hands over exactly those (live bots only, as before).
        for var, val in _resolve_live_exchange_env().items():
            env.setdefault(var, val)

    return env


def _broker_host() -> str:
    """The address a bot should use to reach this API's credential broker.

    Follows the API's bind host: a wildcard bind is reached over the matching
    loopback, a specific address (e.g. ``::1``) directly.
    """
    try:
        from forven.api_security import resolved_bind_host

        host = resolved_bind_host().strip().strip("[]")
    except Exception:
        host = ""
    if host in {"", "0.0.0.0"}:
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _resolve_live_exchange_env() -> dict[str, str]:
    """Hyperliquid credentials from the encrypted Settings store, as env vars."""
    try:
        from forven.exchange.hyperliquid import _load_creds_from_forven_settings

        creds = _load_creds_from_forven_settings() or {}
    except Exception:
        logger.warning("Could not resolve Hyperliquid credentials for a live bot", exc_info=True)
        return {}
    mapping = {
        "HL_API_SECRET": "FORVEN_HL_API_SECRET",
        "HL_API_KEY": "FORVEN_HL_API_KEY",
        "HL_WALLET_ADDRESS": "FORVEN_HL_WALLET_ADDRESS",
        "USE_TESTNET": "FORVEN_HL_USE_TESTNET",
    }
    return {env_var: str(creds[key]) for key, env_var in mapping.items() if creds.get(key)}


def _sandbox_protected_paths() -> list[Path]:
    """Files a sandboxed bot must not be able to read: the master key wherever
    it lives, and .env files."""
    from forven.secret_storage import _legacy_key_path, secret_config_dir

    return [secret_config_dir(), _legacy_key_path(), _REPO_ROOT / ".env", FORVEN_HOME / ".env"]


_sandbox_warned = False


def bot_sandbox_status() -> SandboxStatus | None:
    """The low-integrity sandbox's state, or None when it is off (non-Windows,
    or FORVEN_BOT_SANDBOX=0). Prepares and self-checks it on first use."""
    if not sandbox_enabled():
        return None
    status = ensure_sandbox_ready(FORVEN_HOME, _sandbox_protected_paths(), FORVEN_DB, _REPO_ROOT)
    global _sandbox_warned
    if status.problems and not _sandbox_warned:
        _sandbox_warned = True
        what = "Bot sandbox is on, with caveats" if status.usable else "Bots are starting without the sandbox"
        log_activity(
            "warning", "bot_factory",
            f"{what}: {'; '.join(status.problems)}",
            {"usable": status.usable, "problems": status.problems},
        )
    return status


def _bot_log_path(bot_id: str) -> Path:
    """Get the log file path for a bot subprocess."""
    log_dir = FORVEN_HOME / "logs" / "bots"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"bot-{bot_id}.log"


# Max size of a per-bot log before it's rotated (one .1 backup kept) so long
# soaks don't grow the append-only log unbounded.
_MAX_BOT_LOG_BYTES = 10 * 1024 * 1024


def _rotate_log_if_large(log_path: Path) -> None:
    """Rotate a bot log that has grown past the size cap, keeping one backup."""
    try:
        if log_path.exists() and log_path.stat().st_size > _MAX_BOT_LOG_BYTES:
            backup = log_path.with_suffix(log_path.suffix + ".1")
            try:
                if backup.exists():
                    backup.unlink()
            except Exception:
                pass
            log_path.replace(backup)
    except Exception:
        pass


class BotManager:
    """Manages bot subprocess lifecycles."""

    _instance: BotManager | None = None
    _processes: dict[str, subprocess.Popen]

    def __init__(self):
        self._processes = {}
        self._monitor_task: asyncio.Task | None = None

    @classmethod
    def get_instance(cls) -> BotManager:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def start_bot(self, bot_id: str) -> dict:
        """Spawn a bot subprocess with isolated credentials."""
        bot = get_bot(bot_id)
        if not bot:
            raise ValueError(f"Bot {bot_id} not found")

        # Fail closed on a live-armed bot whose arming state has decayed since
        # go-live: the protective stop config and the per-bot notional ceiling
        # are both load-bearing for live opens.
        if str(bot.get("execution_mode") or "paper").strip().lower() == "live":
            if bot.get("stop_loss_pct") is None:
                raise ValueError(
                    "Live bot has no stop_loss_pct — set one (or switch to paper) before starting."
                )
            try:
                from forven.exchange.risk import get_live_notional_ceilings

                if not get_live_notional_ceilings().get(f"bot:{bot_id}"):
                    raise ValueError(
                        "Live bot has no go-live notional ceiling recorded — re-arm via go-live."
                    )
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(
                    f"Cannot verify the live notional ceiling ({exc}) — refusing to start a live bot."
                )
            wallet_label = str(bot.get("live_wallet") or "").strip().lower()
            if wallet_label:
                try:
                    from forven.exchange import books

                    registered = wallet_label in books.named_wallets()
                except Exception as exc:
                    raise ValueError(
                        f"Cannot verify wallet '{wallet_label}' ({exc}) — refusing to start a live bot."
                    )
                if not registered:
                    raise ValueError(
                        f"Live bot routes to wallet '{wallet_label}' which is no longer "
                        "registered — re-arm via go-live (or restore the wallet)."
                    )

        status = bot.get("runtime_status") or bot.get("status", "stopped")
        if status == "running":
            # LIFE-7: a hard crash can leave a stale 'running' label. If the
            # recorded PID is not actually alive, treat it as stopped and respawn
            # rather than blocking the operator from restarting from the UI.
            existing_pid = bot.get("pid")
            if existing_pid and _is_pid_alive(existing_pid):
                raise ValueError(f"Bot {bot_id} is already running")
            logger.warning(
                "Bot %s marked running but PID %s is dead — clearing stale status and respawning",
                bot_id, existing_pid,
            )
            set_bot_status(bot_id, "stopped")

        env = _build_isolated_env(bot, issue_bot_token(bot_id, resolve_bot_provider(bot)))
        log_path = _bot_log_path(bot_id)
        _rotate_log_if_large(log_path)
        argv = [
            sys.executable, "-m", "forven.bot_factory.runner",
            "--bot-id", bot_id,
            "--parent-pid", str(os.getpid()),
        ]

        # Windows: run the bot at low integrity so it can't read the master key
        # or change Forven's code. Never blocks a start: if the sandbox is
        # unusable here, the bot starts as before (and the activity log says why).
        process = None
        sandboxed = False
        sandbox = bot_sandbox_status()
        if sandbox is not None and sandbox.usable:
            try:
                process = spawn_sandboxed(
                    argv, env=sandbox_env(env, FORVEN_HOME), cwd=_REPO_ROOT, log_path=log_path,
                )
                sandboxed = True
            except Exception as exc:
                logger.warning("Sandboxed start failed for bot %s; starting unsandboxed: %s", bot_id, exc)
        if process is None:
            process = self._spawn_unsandboxed(argv, env, log_path)

        self._processes[bot_id] = process
        set_bot_status(bot_id, "running", pid=process.pid)
        log_activity(
            "info", "bot_factory",
            f"Bot '{bot.get('name', bot_id)}' started (PID {process.pid})" + (" in the sandbox" if sandboxed else ""),
            {"bot_id": bot_id, "pid": process.pid, "sandboxed": sandboxed},
        )

        return {"status": "started", "pid": process.pid, "log_path": str(log_path)}

    @staticmethod
    def _spawn_unsandboxed(argv: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen:
        """Spawn the runner as a normal child process."""
        # H-R1: open the log file, hand it to Popen, then close OUR copy so the
        # FD doesn't leak if we repeatedly start/restart bots. The child keeps
        # its inherited copy. If Popen itself raises, we still close in `finally`.
        log_handle = log_path.open("a", encoding="utf-8")
        popen_kwargs = {
            "env": env,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }

        if os.name == "nt":
            # CREATE_NO_WINDOW only — never combine with DETACHED_PROCESS:
            # Windows ignores CREATE_NO_WINDOW when DETACHED_PROCESS is set, so
            # the child runs console-less and the venv launcher's re-spawned
            # base interpreter allocates a fresh VISIBLE console (terminal
            # window pops up on every bot start). With CREATE_NO_WINDOW alone
            # the child owns a hidden console the grandchild inherits.
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
            popen_kwargs["creationflags"] = creationflags
        else:
            popen_kwargs["start_new_session"] = True

        try:
            return subprocess.Popen(argv, **popen_kwargs)
        finally:
            try:
                log_handle.close()
            except Exception:
                pass

    def stop_bot(self, bot_id: str, timeout: float = 5.0) -> dict:
        """Stop a bot subprocess.

        On Windows, uses kill() directly since terminate() with
        CREATE_NEW_PROCESS_GROUP can be unreliable. Targets only the
        specific PID to avoid killing the parent process.
        """
        process = self._processes.get(bot_id)
        pid = None

        if process and process.poll() is None:
            pid = process.pid
        else:
            from forven.db import get_bot_status as _get_status
            status = _get_status(bot_id)
            if status and status.get("pid"):
                pid = status["pid"]

        if pid and _is_pid_alive(pid):
            # Safety: never kill our own process
            if pid == os.getpid():
                logger.error("Refusing to kill own PID %d for bot %s", pid, bot_id)
            else:
                try:
                    p = psutil.Process(pid)
                    # Verify this is actually a Python/bot process, not something else
                    try:
                        cmdline = p.cmdline()
                        # LIFE-4: require THIS bot's runner argv ("--bot-id <id>")
                        # so a recycled PID that's merely some other forven process
                        # is never killed.
                        looks_like_this_bot = (
                            any("bot_factory" in arg for arg in cmdline)
                            and "--bot-id" in cmdline
                            and bot_id in cmdline
                        )
                        if not looks_like_this_bot:
                            logger.warning(
                                "PID %d doesn't look like bot %s's process (cmdline mismatch), skipping kill",
                                pid, bot_id,
                            )
                        else:
                            p.kill()  # Direct kill — most reliable on Windows
                            try:
                                p.wait(timeout=3)
                            except psutil.TimeoutExpired:
                                pass
                    except (psutil.AccessDenied, psutil.NoSuchProcess):
                        p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        self._processes.pop(bot_id, None)
        set_bot_status(bot_id, "stopped")
        revoke_bot_token(bot_id)

        bot = get_bot(bot_id)
        # LIVE-STOP-1: a stopped bot no longer manages its positions, so leaving
        # a REAL exchange position open (unmanaged) is unsafe and surprising.
        # Flatten the bot's live positions now — the subprocess is already dead,
        # so it can't race the reduce-only closes. Paper bots have no live rows,
        # so this is a no-op for them.
        flattened = self._flatten_live_positions_on_stop(bot, reason="bot_stopped")

        summary = f"Bot '{(bot or {}).get('name', bot_id)}' stopped"
        if flattened:
            closed = sum(1 for r in flattened if r.get("state") == "closed")
            pending = sum(1 for r in flattened if r.get("state") == "pending")
            parts = []
            if closed:
                parts.append(f"{closed} live position(s) closed")
            if pending:
                parts.append(f"{pending} close(s) pending exchange reconcile")
            if parts:
                summary += " — " + ", ".join(parts)
        log_activity(
            "info", "bot_factory", summary,
            {"bot_id": bot_id, "flattened": flattened},
        )
        return {"status": "stopped", "flattened": flattened}

    def _flatten_live_positions_on_stop(self, bot: dict | None, *, reason: str) -> list[dict]:
        """Reduce-only close every OPEN live position the bot holds. Best-effort:
        a single failed close never blocks the stop, and the resting exchange
        stop still protects a position whose close is pending reconcile."""
        if not bot or str(bot.get("execution_mode") or "paper").strip().lower() != "live":
            return []
        try:
            from forven.bot_factory.live_exec import flatten_live_positions

            results = flatten_live_positions(bot, reason=reason)
            if results:
                logger.warning(
                    "Bot %s stop flattened live positions: %s", bot.get("id"), results,
                )
            return results
        except Exception as exc:
            logger.error(
                "Bot %s stop: live-position flatten FAILED: %s", (bot or {}).get("id"), exc,
                exc_info=True,
            )
            return [{"state": "failed", "message": str(exc)}]

    def kill_all(self) -> dict:
        """Stop all running bots immediately."""
        running = get_running_bots()
        stopped = 0
        for bot_info in running:
            try:
                self.stop_bot(bot_info["bot_id"], timeout=5.0)
                stopped += 1
            except Exception as e:
                logger.error("Failed to stop bot %s: %s", bot_info["bot_id"], e)
        log_activity(
            "warning", "bot_factory",
            f"Kill-all: stopped {stopped} bots",
            {"stopped_count": stopped},
        )
        return {"stopped": stopped}

    async def monitor_bots(self) -> None:
        """Background loop that checks heartbeats and marks stale bots as error."""
        from datetime import datetime, timezone

        cycle = 0
        while True:
            try:
                cycle += 1
                running = get_running_bots()
                now = datetime.now(timezone.utc)

                for bot_info in running:
                    bot_id = bot_info["bot_id"]
                    pid = bot_info.get("pid")
                    last_hb = bot_info.get("last_heartbeat")

                    # Check if PID is alive
                    if pid and not _is_pid_alive(pid):
                        logger.warning("Bot %s PID %s is dead", bot_id, pid)
                        set_bot_status(bot_id, "error", error_message="Process died unexpectedly")
                        revoke_bot_token(bot_id)
                        log_activity(
                            "error", "bot_factory",
                            f"Bot '{bot_info.get('name', bot_id)}' process died (PID {pid})",
                            {"bot_id": bot_id, "pid": pid},
                        )
                        self._processes.pop(bot_id, None)
                        continue

                    # Check heartbeat staleness
                    if last_hb:
                        try:
                            hb_time = datetime.fromisoformat(last_hb.replace("Z", "+00:00"))
                            age = (now - hb_time).total_seconds()
                            if age > _HEARTBEAT_STALE_SECONDS:
                                logger.warning(
                                    "Bot %s heartbeat stale (%.0fs old)", bot_id, age
                                )
                                # LIFE-3: the process may be alive but wedged —
                                # kill it and drop our handle so recovery respawns
                                # a clean process instead of an untracked zombie.
                                if pid and pid != os.getpid() and _is_pid_alive(pid):
                                    try:
                                        psutil.Process(pid).kill()
                                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                                        pass
                                self._processes.pop(bot_id, None)
                                set_bot_status(
                                    bot_id, "error",
                                    error_message=f"Heartbeat stale ({int(age)}s)",
                                )
                                revoke_bot_token(bot_id)
                                log_activity(
                                    "warning", "bot_factory",
                                    f"Bot '{bot_info.get('name', bot_id)}' heartbeat stale ({int(age)}s)",
                                    {"bot_id": bot_id, "stale_seconds": int(age)},
                                )
                        except (ValueError, TypeError):
                            pass

                # PERSIST-2: periodically close orphaned bot trades (deleted or
                # never-recovered bots) so phantom OPEN paper rows don't linger
                # between restarts. Every ~10 min (20 × 30s).
                if cycle % 20 == 0:
                    self._reconcile_orphans()

            except Exception as e:
                logger.error("Bot monitor error: %s", e)

            await asyncio.sleep(_MONITOR_INTERVAL)

    def _reconcile_orphans(self) -> None:
        """Close OPEN paper trades whose owning bot is neither tracked here nor
        alive on its recorded PID. Shared by the startup recovery and the
        periodic monitor sweep."""
        from forven.db import get_db, reconcile_orphaned_bot_trades

        try:
            with get_db() as conn:
                alive_rows = conn.execute(
                    "SELECT bot_id, pid FROM bot_status WHERE status = 'running' AND pid IS NOT NULL"
                ).fetchall()
            still_alive = {
                r["bot_id"] for r in alive_rows if r["pid"] and _is_pid_alive(r["pid"])
            }
            active_ids = set(self._processes.keys()) | still_alive
            orphans = reconcile_orphaned_bot_trades(active_bot_ids=active_ids)
            if orphans:
                logger.info(
                    "Periodic orphan reconcile: closed %d bot trade(s) for inactive bots",
                    len(orphans),
                )
        except Exception as e:
            logger.error("Periodic orphan reconcile failed: %s", e)

    def recover_bots(self) -> dict:
        """On startup, recover bots that should be running.

        Recovers bots with status 'running' (crash mid-run), 'error'
        (heartbeat stale / process died), or that were stopped by
        shutdown_all during a clean restart. Uses a separate flag
        to track which bots were intentionally running.
        """
        from forven.db import get_db

        # Find all bots that were running or errored (not manually stopped)
        with get_db() as conn:
            rows = conn.execute(
                """SELECT s.bot_id, s.pid, s.status, c.name
                   FROM bot_status s
                   JOIN bot_configs c ON s.bot_id = c.id
                   WHERE s.status IN ('running', 'error')
                      OR (s.status = 'stopped' AND s.started_at IS NOT NULL
                          AND s.pid IS NOT NULL)"""
            ).fetchall()
            candidates = [dict(r) for r in rows]

        recovered = 0
        cleaned = 0

        for bot_info in candidates:
            bot_id = bot_info["bot_id"]
            pid = bot_info.get("pid")

            if pid and _is_pid_alive(pid):
                logger.info("Bot %s is still alive (PID %s), skipping recovery", bot_id, pid)
                continue

            # Bot was running but process is dead — re-spawn
            try:
                logger.info("Recovering bot %s (status: %s, old PID %s)", bot_id, bot_info.get("status"), pid)
                set_bot_status(bot_id, "stopped")
                self.start_bot(bot_id)
                recovered += 1
                log_activity(
                    "info", "bot_factory",
                    f"Bot '{bot_info.get('name', bot_id)}' auto-recovered after restart",
                    {"bot_id": bot_id},
                )
            except Exception as e:
                logger.error("Failed to recover bot %s: %s", bot_id, e)
                set_bot_status(bot_id, "error", error_message=f"Recovery failed: {e}")
                cleaned += 1

        # Close OPEN trades whose bot we did not bring back. After
        # recovery, `self._processes` has every bot we expect to be live.
        # Also include every still-alive pre-existing PID — those are bots
        # we didn't touch but that are still running the previous process.
        try:
            with get_db() as conn:
                alive_rows = conn.execute(
                    """SELECT bot_id, pid FROM bot_status
                        WHERE status = 'running' AND pid IS NOT NULL"""
                ).fetchall()
            still_alive = {
                r["bot_id"] for r in alive_rows
                if r["pid"] and _is_pid_alive(r["pid"])
            }
            # LIFE-5: also spare bots we ATTEMPTED to recover this startup (even
            # if respawn failed transiently) so a flaky start doesn't force-close
            # their positions. The periodic monitor reconcile catches any that
            # stay dead.
            attempted_ids = {c["bot_id"] for c in candidates}
            active_ids = set(self._processes.keys()) | still_alive | attempted_ids
            orphans = reconcile_orphaned_bot_trades(active_bot_ids=active_ids)
            if orphans:
                logger.info(
                    "Orphan reconcile on startup: closed %d trade(s) for bots not recovered",
                    len(orphans),
                )
                log_activity(
                    "warning", "bot_factory",
                    f"Orphan reconcile: closed {len(orphans)} bot trades at startup",
                    {"count": len(orphans)},
                )
        except Exception as e:
            logger.error("Orphan reconcile failed: %s", e)

        return {"recovered": recovered, "failed": cleaned}

    def shutdown_all(self) -> None:
        """Gracefully stop all bot processes during server shutdown.

        Kills processes but does NOT update DB status — so that
        recover_bots() on next startup sees them as needing restart.
        """
        running = get_running_bots()
        for bot_info in running:
            bot_id = bot_info["bot_id"]
            pid = bot_info.get("pid")
            if pid and _is_pid_alive(pid):
                try:
                    p = psutil.Process(pid)
                    # LIFE-8: give the bot a chance to drain gracefully. The
                    # runner handles SIGBREAK (Windows) / SIGTERM (POSIX) by
                    # setting its _shutdown flag; fall back to a hard kill.
                    try:
                        if os.name == "nt":
                            import signal as _signal
                            os.kill(pid, _signal.CTRL_BREAK_EVENT)
                        else:
                            p.terminate()
                        p.wait(timeout=3.0)
                    except (psutil.TimeoutExpired, ProcessLookupError, OSError):
                        try:
                            p.kill()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self._processes.pop(bot_id, None)
