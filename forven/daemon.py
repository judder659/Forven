"""Forven Trading Daemon — data ingestion and hard-risk loop.

Responsibilities:
- Ingest live market prices (WS + fallback polling)
- Publish a shared price snapshot cache for scanner workers
- Enforce hard risk controls (kill-switch, daily-loss halt)
- Reconcile exchange/account state and emit heartbeats

Non-responsibilities:
- Strategy signal generation
- Strategy entry/exit decisions
- Scanner execution scheduling
"""

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timezone
from uuid import uuid4

from forven.async_utils import spawn

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None

from forven.config import FORVEN_HOME, ensure_dirs, get_execution_mode
from forven.db import get_db, get_open_trades, init_db, kv_get, kv_set_best_effort, log_activity
from forven.exchange.hyperliquid import (
    HyperLiquidFeed,
    _get_creds,
    get_account_value,
    get_all_mids,
    get_open_orders,
    get_positions,
    resolve_configured_testnet,
)
from forven.exchange.risk import (
    close_all_positions,
    get_risk_status,
    reconcile_all_books,
    sync_from_trades,
    update_equity,
)
from forven.market_cache import normalize_prices, publish_price_snapshot, publish_candle_snapshot
from forven.market_data import (
    fetch_market_candles,
    fetch_binance_prices,
    resolve_market_data_source,
    BinancePriceFeed,
    dataframe_to_ohlcv_rows,
)
from forven.runtime_health import compute_runtime_code_fingerprint
from forven.system_mode_policy import autonomous_runtime_allowed

log = logging.getLogger("forven.daemon")


def _get_testnet() -> bool:
    """Resolve HyperLiquid testnet preference, defaulting to the shared exchange helper."""
    def _truthy(value: object) -> bool:
        return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}

    try:
        creds = _get_creds()
    except Exception:
        creds = None
    if isinstance(creds, dict):
        raw = creds.get("USE_TESTNET")
        if raw is not None and str(raw).strip():
            return _truthy(raw)

    try:
        settings = kv_get("forven:settings", {}) or {}
    except Exception:
        settings = {}
    if isinstance(settings, dict) and settings.get("hyperliquid_testnet") is not None:
        return _truthy(settings.get("hyperliquid_testnet"))

    return resolve_configured_testnet(default_testnet=True)


# Data/Risk loop config
_BASE_COINS = ["ETH", "BTC", "SOL"]


def _normalize_coin_symbol(value: object) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return ""
    for separator in ("/", "-", "_"):
        if separator in raw:
            raw = raw.split(separator, 1)[0]
            break
    for suffix in ("USDT", "USD", "PERP"):
        if raw.endswith(suffix) and len(raw) > len(suffix):
            raw = raw[: -len(suffix)]
            break
    return raw.strip()


def _active_coins() -> list[str]:
    """Return base coins plus assets from open trades and active strategies."""
    coins = set(_BASE_COINS)
    try:
        for trade in get_open_trades():
            asset = _normalize_coin_symbol(trade.get("asset") or trade.get("symbol"))
            if asset:
                coins.add(asset)
    except Exception:
        pass
    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT symbol
                FROM strategies
                WHERE LOWER(COALESCE(stage, status, '')) LIKE 'paper%'
                   OR LOWER(COALESCE(stage, status, '')) LIKE 'live%'
                   OR LOWER(COALESCE(stage, status, '')) LIKE 'deploy%'
                   OR LOWER(COALESCE(status, '')) LIKE 'paper%'
                   OR LOWER(COALESCE(status, '')) LIKE 'live%'
                   OR LOWER(COALESCE(status, '')) LIKE 'deploy%'
                """
            ).fetchall()
        for row in rows:
            asset = _normalize_coin_symbol(row["symbol"] if hasattr(row, "keys") else row[0])
            if asset:
                coins.add(asset)
    except Exception:
        pass
    return sorted(coins)

HEARTBEAT_INTERVAL = 300  # 5 minutes
PRICE_FALLBACK_POLL_INTERVAL = 15  # seconds
CANDLE_CACHE_REFRESH_INTERVAL = 90  # seconds
CANDLE_CACHE_BARS = 360
# H7: minimum seconds between liquidation-distance sweeps (bounds REST load on
# the shared account breaker since _run_tick is not itself interval-gated).
LIQ_CHECK_INTERVAL_SECONDS = 60
_LAST_LIQ_CHECK = [0.0]
# Resting-order mark watcher: minimum seconds between armed stop/TP checks (WS
# ticks can arrive many times per second; level checks don't need sub-second
# granularity) and the bounded time for a check + any resulting DB closes.
MARK_WATCH_MIN_INTERVAL_SECONDS = 1.0
MARK_WATCH_TIMEOUT_SECONDS = 15.0
_LAST_MARK_WATCH = [0.0]
# KS-1: while the kill-switch is persisted-active, re-flatten any residual
# position on this cadence (a missed/partial/failed first flatten, a restart
# between state-persist and flatten, or a close timeout) instead of relying on
# the one-shot transition tick. Throttled so an unfillable position can't hot-loop.
KILL_REFLATTEN_INTERVAL_SECONDS = 60
_LAST_KILL_REFLATTEN = [0.0]
# Last-known-good per-account equity, used to ride out a transient sub-account
# read that returns 0/fails so the books-aggregate can't fake a drawdown and
# trip the kill-switch on a glitch (keyed by lowercased address; master="__master__").
_BOOK_EQUITY_CACHE: dict[str, float] = {}
# PNL-2: guard the books-disabled fast path against a transient books_enabled()
# flip. Once books are confirmed ON, a single False read (e.g. a kv glitch) must
# not crater the aggregate to a master-only value and fake a drawdown; require
# books-off to persist for a few ticks before trusting the master-only path.
_LAST_BOOKS_ENABLED: bool = False
_BOOKS_DISABLED_STREAK: int = 0
_BOOKS_OFF_CONFIRM_TICKS: int = 3


def _env_float(name: str, default: float, minimum: float = 0.1) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return max(minimum, float(default))
        return max(minimum, float(raw))
    except Exception:
        return max(minimum, float(default))


PRICE_SNAPSHOT_TIMEOUT_SECONDS = _env_float("FORVEN_DAEMON_PRICE_SNAPSHOT_TIMEOUT_SECONDS", 6)
RISK_ACCOUNT_TIMEOUT_SECONDS = _env_float("FORVEN_DAEMON_ACCOUNT_TIMEOUT_SECONDS", 10)
RISK_UPDATE_TIMEOUT_SECONDS = _env_float("FORVEN_DAEMON_RISK_UPDATE_TIMEOUT_SECONDS", 8)
# PER-ACCOUNT reconcile budget. reconcile_all_books runs one sequential pass per
# wallet (master + direction books + named wallets + any open-trade-routed
# account), each making several exchange round-trips — a fixed total budget
# sized for one wallet started "timing out" the moment the operator registered
# a second account, and 15 minutes of those false timeouts tripped the
# sustained-outage operator halt against a perfectly healthy exchange. See
# _reconcile_timeout_seconds for the account-count scaling.
RISK_RECONCILE_TIMEOUT_SECONDS = _env_float("FORVEN_DAEMON_RECONCILE_TIMEOUT_SECONDS", 20)


def _reconcile_timeout_seconds() -> float:
    """Total reconcile budget = per-account budget × (accounts + 1 headroom)."""
    accounts = 1
    try:
        from forven.exchange import books

        accounts = max(1, len(books.active_book_addresses()))
    except Exception:
        accounts = 1
    return RISK_RECONCILE_TIMEOUT_SECONDS * (accounts + 1)
# CR-2: a single transient reconcile error/timeout (testnet REST blip) must not
# freeze ALL new entries + flag 'requires operator'. Only escalate to a hard
# block after this many CONSECUTIVE errors; real discrepancies still block at once.
_RECONCILE_ERROR_ESCALATE_AFTER = int(_env_float("FORVEN_DAEMON_RECONCILE_ERROR_ESCALATE_AFTER", 3))
# After a transient error, retry on this short cadence instead of the full 600s.
_RECONCILE_ERROR_RETRY_SECONDS = _env_float("FORVEN_DAEMON_RECONCILE_ERROR_RETRY_SECONDS", 45)
# A pure CONNECTIVITY/read failure ("could not fetch exchange positions" — a 504
# burst tripping the breaker, a REST timeout) is NOT a divergence: we have no
# information about exchange state, so it must NOT latch an operator-required hard
# halt that freezes unattended trading on a transient testnet blip. While the
# exchange is unreachable, new live opens are already fail-closed per-call by
# can_open Rule 0c (it cannot verify margin) and existing positions keep their
# on-exchange protective stops, so a brief outage is safe to ride out silently.
# We only escalate to a hard halt + operator alert after a SUSTAINED outage,
# measured by ELAPSED TIME rather than a small consecutive-error count (the count
# is why a normal testnet 504 burst hard-halted overnight). This is the sole
# remaining outage detector after the breaker is made 504-tolerant (FIX 1b).
_RECONCILE_OUTAGE_ESCALATE_SECONDS = _env_float(
    "FORVEN_DAEMON_RECONCILE_OUTAGE_ESCALATE_SECONDS", 900
)
# Substrings that mark a reconcile error as a connectivity/read failure rather
# than a real DB-vs-exchange divergence.
_RECONCILE_CONNECTIVITY_MARKERS = (
    "could not fetch exchange positions",
    "circuit breaker",
    "gateway timeout",
    "bad gateway",
    "service unavailable",
    "timed out",
    "timeout",
    "connection",
    "temporarily unavailable",
    " 502",
    " 503",
    " 504",
)
RISK_CLOSE_TIMEOUT_SECONDS = _env_float("FORVEN_DAEMON_CLOSE_TIMEOUT_SECONDS", 20)
HEARTBEAT_SEND_TIMEOUT_SECONDS = _env_float("FORVEN_DAEMON_HEARTBEAT_TIMEOUT_SECONDS", 20)
FALLBACK_MIDS_TIMEOUT_SECONDS = _env_float("FORVEN_DAEMON_MIDS_TIMEOUT_SECONDS", 8)
CANDLE_FETCH_TIMEOUT_SECONDS = _env_float("FORVEN_DAEMON_CANDLE_TIMEOUT_SECONDS", 12)
SHUTDOWN_GRACE_SECONDS = _env_float("FORVEN_DAEMON_SHUTDOWN_GRACE_SECONDS", 5)

shutdown = asyncio.Event()
daemon_lock_fd: int | None = None

# Lock byte offset — must NOT overlap with PID data at offset 0.
# On Windows, msvcrt.locking places a mandatory lock that prevents reads of the
# locked region. Offset 1024 keeps the PID readable by start_all.ps1.
_DAEMON_LOCK_BYTE_OFFSET = 1024
_task_warning_last_logged_at: dict[str, float] = {}
_TASK_WARNING_THROTTLE_SECONDS = 60.0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_recovery_state() -> dict[str, object]:
    return {
        "recovery_active": False,
        "recovery_status": "idle",
        "recovery_started_at": None,
        "recovery_position_count": 0,
        "recovery_discrepancy_count": 0,
        "recovery_requires_operator": False,
        "recovery_batch_id": None,
        "recovery_summary": "",
        "recovery_open_order_count": 0,
        "recovery_last_checked_at": None,
        "recovery_network": None,
    }


def _set_recovery_state(state: dict, **updates) -> dict[str, object]:
    payload = _default_recovery_state()
    for key in payload:
        if key in state:
            payload[key] = state.get(key)
    payload.update(updates)
    for key, value in payload.items():
        state[key] = value
    return payload


def mark_boot_recovery_pending() -> None:
    """BOOT-1: force the startup-recovery gate CLOSED at process start, BEFORE the
    scheduler/scanner thread can run a live execution scan.

    A cleanly-exited prior run persists recovery_active=False; without this stamp the
    scanner (a separate thread that starts with the same delay) could open a live
    position in the boot window BEFORE the daemon's boot reconcile re-verifies the
    exchange. The daemon's reconcile clears/re-sets it on the real outcome; a
    daemon-spawn failure clears it via clear_boot_recovery_pending. Best-effort.
    """
    try:
        raw = kv_get("daemon_state", {}) or {}
        state = dict(raw) if isinstance(raw, dict) else {}
        # Leave an already-active genuine operator block (blocked/error) intact.
        if bool(state.get("recovery_active")) and str(state.get("recovery_status") or "").strip().lower() in {"blocked", "error"}:
            return
        _set_recovery_state(
            state,
            recovery_active=True,
            recovery_status="checking",
            recovery_started_at=_iso_now(),
            recovery_summary="Startup exchange reconcile pending — new entries blocked until the boot reconcile verifies the exchange.",
        )
        kv_set_best_effort("daemon_state", state)
        log.info("BOOT-1: stamped startup-recovery gate (checking) before runtime threads start.")
    except Exception as exc:
        log.warning("Could not stamp boot recovery gate: %s", exc)


def clear_boot_recovery_pending(reason: str = "daemon_unavailable") -> None:
    """Release the BOOT-1 startup-recovery gate when the daemon will NOT run in this
    process (its boot reconcile would otherwise never clear the 'checking' stamp,
    wedging paper trading). Only clears a 'checking' stamp — never an operator
    'blocked'/'error' state."""
    try:
        raw = kv_get("daemon_state", {}) or {}
        state = dict(raw) if isinstance(raw, dict) else {}
        if str(state.get("recovery_status") or "").strip().lower() != "checking":
            return
        _set_recovery_state(
            state,
            recovery_active=False,
            recovery_status=str(reason or "daemon_unavailable"),
            recovery_last_checked_at=_iso_now(),
            recovery_summary="Startup recovery gate released (daemon not running this process).",
        )
        kv_set_best_effort("daemon_state", state)
        log.info("BOOT-1: released startup-recovery gate (%s).", reason)
    except Exception as exc:
        log.warning("Could not clear boot recovery gate: %s", exc)


def publish_recovery_operator_state(state: dict, *, action_key: str = "exchange_recovery") -> dict[str, object]:
    recovery = _default_recovery_state()
    for key in recovery:
        recovery[key] = state.get(key, recovery[key])

    status = str(recovery.get("recovery_status") or "idle").strip().lower() or "idle"
    action_status = "ok"
    if status == "error":
        action_status = "fail"
    elif bool(recovery.get("recovery_active")) or bool(recovery.get("recovery_requires_operator")):
        action_status = "warn"

    summary = str(recovery.get("recovery_summary") or "").strip()
    if not summary:
        network = str(recovery.get("recovery_network") or "unknown")
        summary = f"Hyperliquid {network} recovery state is {status}."

    details = {
        "active": bool(recovery.get("recovery_active")),
        "status": status,
        "started_at": recovery.get("recovery_started_at"),
        "position_count": int(recovery.get("recovery_position_count", 0) or 0),
        "discrepancy_count": int(recovery.get("recovery_discrepancy_count", 0) or 0),
        "requires_operator": bool(recovery.get("recovery_requires_operator", False)),
        "batch_id": recovery.get("recovery_batch_id"),
        "summary": summary,
        "open_order_count": int(recovery.get("recovery_open_order_count", 0) or 0),
        "last_checked_at": recovery.get("recovery_last_checked_at"),
        "network": recovery.get("recovery_network"),
    }

    raw_state = kv_get("ops_manual_action_state", {}) or {}
    operator_state = raw_state if isinstance(raw_state, dict) else {}
    operator_state[str(action_key).strip() or "exchange_recovery"] = {
        "status": action_status,
        "summary": summary,
        "updated_at": _iso_now(),
        "details": details,
    }
    kv_set_best_effort("ops_manual_action_state", operator_state)
    return details


def _persist_daemon_state(state: dict) -> dict[str, object]:
    kv_set_best_effort("daemon_state", state)
    publish_recovery_operator_state(state)
    return state


def _network_label(testnet: bool) -> str:
    return "testnet" if bool(testnet) else "mainnet"


def _is_connectivity_error_text(text: object) -> bool:
    """True when an error string looks like a connectivity/read failure (not a
    DB-vs-exchange divergence)."""
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return any(marker in lowered for marker in _RECONCILE_CONNECTIVITY_MARKERS)


def _is_reconcile_fetch_unavailable(recon: object) -> bool:
    """Classify a reconcile result as a connectivity/read failure.

    Prefers the explicit error_kind tag stamped by reconcile_exchange_positions
    (error_kind='fetch_unavailable'); falls back to matching the error message so
    the daemon-constructed timeout/exception errors (which carry no tag) are also
    recognised. NEVER treats a result that carries real discrepancies as a fetch
    failure — a divergence must keep the immediate hard-halt path.
    """
    if not isinstance(recon, dict):
        return False
    if recon.get("discrepancies"):
        return False
    if str(recon.get("error_kind") or "").strip().lower() == "fetch_unavailable":
        return True
    return _is_connectivity_error_text(recon.get("error"))


def _seconds_since(iso_ts: object) -> float:
    """Elapsed seconds since an ISO-8601 timestamp; 0.0 if unparseable."""
    raw = str(iso_ts or "").strip()
    if not raw:
        return 0.0
    try:
        then = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - then).total_seconds())


def _count_exchange_positions(payload: dict | None) -> int:
    if not isinstance(payload, dict):
        return 0
    count = 0
    for raw_position in payload.get("positions", []):
        position = raw_position.get("position", raw_position) if isinstance(raw_position, dict) else {}
        try:
            signed_size = float(position.get("szi", 0) or 0)
        except Exception:
            signed_size = 0.0
        if signed_size != 0:
            count += 1
    return count


def _normalize_exchange_account_snapshot(
    account_snapshot: dict | None,
    *,
    testnet: bool,
    synced_at: str | None = None,
) -> dict[str, object] | None:
    if not isinstance(account_snapshot, dict):
        return None
    synced = str(synced_at or _iso_now())
    snapshot = {
        "accountValue": float(account_snapshot.get("accountValue", 0) or 0),
        "totalMarginUsed": float(account_snapshot.get("totalMarginUsed", 0) or 0),
        "totalNtlPos": float(account_snapshot.get("totalNtlPos", 0) or 0),
        "withdrawable": float(
            account_snapshot.get("withdrawable", account_snapshot.get("totalRawUsd", 0)) or 0
        ),
        "source": str(account_snapshot.get("source") or "exchange"),
        "network": _network_label(testnet),
        # BOOK-BUDGET-1: per-wallet equity breakdown ({label: usd}), when known.
        "books": dict(account_snapshot.get("books") or {}) if isinstance(account_snapshot.get("books"), dict) else {},
        "synced_at": synced,
    }
    return snapshot


def _persist_exchange_account_snapshot(
    state: dict,
    account_snapshot: dict | None,
    *,
    testnet: bool,
    synced_at: str | None = None,
) -> dict[str, object] | None:
    normalized = _normalize_exchange_account_snapshot(
        account_snapshot,
        testnet=testnet,
        synced_at=synced_at,
    )
    if not normalized:
        return None
    state["exchange_account"] = dict(normalized)
    account_equity = float(normalized.get("accountValue", 0) or 0)
    if account_equity > 0:
        state["account_equity"] = account_equity
    state["account_equity_synced_at"] = normalized.get("synced_at")
    return normalized


def _exchange_credentials_status() -> tuple[bool, str | None]:
    try:
        _get_creds()
        return True, None
    except Exception as exc:
        return False, str(exc)


def _read_lock_pid(lock_path) -> int | None:
    """Read PID from daemon lock file if present and parseable."""
    try:
        raw = lock_path.read_text().strip()
        if not raw:
            return None
        pid = int(raw)
        return pid if pid > 0 else None
    except Exception:
        return None


def _is_pid_running(pid: int | None) -> bool:
    """Check whether a PID appears to still be alive."""
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
    except OSError:
        return False
    except Exception:
        return False


def _sweep_orphan_multiprocessing_workers() -> int:
    """Kill zombie multiprocessing spawn_main workers whose parent process is dead.

    Why: on Windows, taskkill /F or an unclean crash of a forven process does not terminate
    its `ProcessPoolExecutor` children (spawn-context workers). Those orphans hold open
    SQLite connections with pinned WAL reader snapshots, which blocks WAL checkpoint. The
    WAL file grows unbounded and writers eventually fail with "database is locked".

    How to apply: call once at daemon startup, before init_db(), so we clear any readers
    pinning old snapshots from a prior crashed run.
    """
    try:
        import psutil  # type: ignore
    except ImportError:
        return 0

    killed = 0
    own_pid = os.getpid()
    try:
        for proc in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
            try:
                info = proc.info
                pid = info.get("pid")
                if pid == own_pid:
                    continue
                name = (info.get("name") or "").lower()
                if not name.startswith("python"):
                    continue
                cmdline = " ".join(info.get("cmdline") or [])
                if "multiprocessing.spawn" not in cmdline and "multiprocessing-fork" not in cmdline:
                    continue
                ppid = info.get("ppid")
                if _is_pid_running(ppid):
                    continue
                proc.kill()
                killed += 1
                log.warning(
                    "Sweeper killed orphan mp worker PID %s (dead parent %s)",
                    pid, ppid,
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as err:
                log.debug("Sweeper could not inspect process: %s", err)
    except Exception as err:
        log.warning("Orphan mp sweeper failed: %s", err)
    return killed


def _is_daemon_lock_held(lock_path) -> bool:
    """Check whether the daemon singleton lock is currently held."""
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
                os.lseek(fd, _DAEMON_LOCK_BYTE_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                acquired = True
                return False
            except (IOError, OSError):
                return True
        return False
    finally:
        if acquired:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                elif msvcrt is not None:
                    os.lseek(fd, _DAEMON_LOCK_BYTE_OFFSET, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
        try:
            os.close(fd)
        except Exception:
            pass


def get_daemon_lock_status() -> dict[str, object]:
    """Return singleton lock health for the daemon process."""
    current_pid = os.getpid()
    lock_path = FORVEN_HOME / "daemon.lock"
    supported = fcntl is not None or msvcrt is not None

    status = {
        "singleton_supported": supported,
        "singleton_enforced": supported,
        "lock_path": str(lock_path),
        "current_pid": current_pid,
        "held_by_current_process": bool(daemon_lock_fd is not None),
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
    if daemon_lock_fd is not None:
        status["lock_held"] = True
        status["active_pid"] = current_pid
        status["active_pid_running"] = True
        return status

    lock_held = _is_daemon_lock_held(lock_path)
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


def _coerce_bounded_int(value, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = int(default)
    return max(minimum, min(maximum, parsed))


def _runtime_candle_refresh_interval() -> int:
    settings = kv_get("forven:settings", {})
    payload = settings if isinstance(settings, dict) else {}
    return _coerce_bounded_int(
        payload.get("daemon_candle_cache_refresh_seconds"),
        CANDLE_CACHE_REFRESH_INTERVAL,
        15,
        3600,
    )


async def _to_thread_with_timeout(task_name: str, timeout_seconds: float, fn, *args, **kwargs):
    """Run a sync function in a thread with bounded timeout."""
    timeout = max(0.1, float(timeout_seconds or 0.1))
    now = time.time()
    last_logged_at = float(_task_warning_last_logged_at.get(task_name, 0.0) or 0.0)
    should_log = (now - last_logged_at) >= _TASK_WARNING_THROTTLE_SECONDS
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, *args, **kwargs),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        if should_log:
            _task_warning_last_logged_at[task_name] = now
            log.warning("%s timed out after %.1fs", task_name, timeout)
        return None
    except Exception as exc:
        if should_log:
            _task_warning_last_logged_at[task_name] = now
            log.warning("%s failed: %s", task_name, exc)
        return None


def _acquire_daemon_lock() -> bool:
    """Acquire a cross-process singleton lock for the daemon loop."""
    global daemon_lock_fd

    if daemon_lock_fd is not None:
        return True

    ensure_dirs()
    lock_path = FORVEN_HOME / "daemon.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)

    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return False
    elif msvcrt is not None:
        try:
            os.lseek(fd, _DAEMON_LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except (IOError, OSError):
            os.close(fd)
            return False
    else:
        log.warning("No file locking available; daemon singleton lock disabled.")
        os.close(fd)
        return True

    # Write PID at offset 0 (readable by other processes since lock is at offset 1024)
    try:
        os.ftruncate(fd, 0)
    except OSError:
        pass
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(os.getpid()).encode("utf-8"))
    daemon_lock_fd = fd
    return True


def _release_daemon_lock() -> None:
    global daemon_lock_fd

    if daemon_lock_fd is None:
        return
    try:
        if fcntl is not None:
            fcntl.flock(daemon_lock_fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            os.lseek(daemon_lock_fd, _DAEMON_LOCK_BYTE_OFFSET, os.SEEK_SET)
            msvcrt.locking(daemon_lock_fd, msvcrt.LK_UNLCK, 1)
    except Exception:
        pass
    try:
        os.close(daemon_lock_fd)
    except Exception:
        pass
    daemon_lock_fd = None
    try:
        lock_path = FORVEN_HOME / "daemon.lock"
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        pass


# --- Discord heartbeat ---


def send_heartbeat(state: dict, prices: dict):
    """Record daemon heartbeat locally without spamming Discord."""
    with get_db() as conn:
        open_c = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status='OPEN'").fetchone()["c"]
        closed_c = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status='CLOSED'").fetchone()["c"]

    sent_msg = ""
    try:
        from forven.strategies.sentiment import analyze_sentiment

        sent = analyze_sentiment()
        sent_msg = f"\nSentiment: {sent['composite']} ({sent['interpretation']}) | F&G: {sent['fng']['score']}"
        state["sentiment"] = sent
    except Exception as e:
        log.warning("Sentiment failed: %s", e)

    regime_msg = ""
    try:
        from forven.regime import detect_all_regimes

        regimes = detect_all_regimes()
        regime_parts = [f"{a}={r.regime}" for a, r in regimes.items()]
        regime_msg = f"\nRegime: {' | '.join(regime_parts)}"
    except Exception:
        pass

    uptime = int(time.time() - state.get("start_ts", time.time()))
    msg = (
        f"FORVEN HEARTBEAT — {datetime.now(timezone.utc).strftime('%H:%M UTC')}\n"
        f"Uptime: {uptime // 60}m | Ticks: {state.get('tick_count', 0)} | "
        f"Trades: {open_c} open, {closed_c} closed\n"
        f"BTC=${prices.get('BTC', '?'):,} ETH=${prices.get('ETH', '?'):,} SOL=${prices.get('SOL', '?'):,}"
        f"{sent_msg}{regime_msg}"
    )

    log_activity("info", "daemon", f"Heartbeat | ticks={state.get('tick_count', 0)} | open={open_c}", {"message": msg})


def run_startup_recovery_preflight(state: dict) -> dict[str, object]:
    """Verify exchange state before the daemon loop starts accepting new entries."""
    mode = str(get_execution_mode() or "paper").strip().lower()
    testnet = _get_testnet()
    network = _network_label(testnet)
    batch_id = f"startup-{uuid4().hex[:12]}"
    started_at = _iso_now()

    # CR-1 carry-forward: a genuine block (real divergence / sustained outage)
    # persisted across the restart must NOT be silently cleared if the FIRST boot
    # reconcile then hits a transient 504 — that would defeat CR-1. Capture it
    # before the "checking" overwrite below so the connectivity branches can
    # preserve it. ('exchange_unreachable'/'checking'/'ok'/'resolved' are NOT
    # genuine blocks.)
    prior_genuine_block = bool(state.get("recovery_active")) and (
        str(state.get("recovery_status") or "").strip().lower() in {"blocked", "error"}
        or int(state.get("recovery_discrepancy_count", 0) or 0) > 0
    )

    _set_recovery_state(
        state,
        recovery_active=True,
        recovery_status="checking",
        recovery_started_at=started_at,
        recovery_position_count=0,
        recovery_discrepancy_count=0,
        recovery_requires_operator=False,
        recovery_batch_id=batch_id,
        recovery_summary=f"Startup recovery check running against Hyperliquid {network}.",
        recovery_open_order_count=0,
        recovery_last_checked_at=started_at,
        recovery_network=network,
    )

    creds_available, creds_error = _exchange_credentials_status()
    if not creds_available:
        if mode == "paper":
            return _set_recovery_state(
                state,
                recovery_active=False,
                recovery_status="skipped_no_credentials",
                recovery_requires_operator=False,
                recovery_summary=(
                    f"Startup recovery skipped on Hyperliquid {network}: "
                    "credentials not configured for exchange-backed paper trading."
                ),
                recovery_last_checked_at=_iso_now(),
            )
        return _set_recovery_state(
            state,
            recovery_active=True,
            recovery_status="error",
            recovery_requires_operator=True,
            recovery_summary=(
                f"Startup recovery could not verify Hyperliquid {network}: "
                f"{creds_error or 'missing credentials'}"
            ),
            recovery_last_checked_at=_iso_now(),
        )

    try:
        positions_payload = get_positions(testnet=testnet)
        open_orders_payload = get_open_orders(testnet=testnet)
        account_snapshot = get_account_value(
            testnet=testnet,
            require_connection=mode in {"live", "mainnet"},
        )
        sync_from_trades()
        recon = reconcile_all_books(
            testnet=testnet,
            adopt_missing_in_sqlite=True,
            open_orders=open_orders_payload,
            recovery_batch_id=batch_id,
        )
        if isinstance(recon, dict) and int(recon.get("adopted_count", 0) or 0) > 0:
            sync_from_trades()
    except Exception as exc:
        # A transient connectivity error on the FIRST boot reconcile (e.g. a
        # testnet 504 burst that tripped the breaker) must NOT latch an
        # operator-required halt that survives the restart — new live opens are
        # already fail-closed per-call by can_open Rule 0c while the exchange is
        # unreachable, and the periodic reconcile will retry on the short cadence.
        # Only a genuine (non-connectivity) preflight error stays a hard halt.
        # A carried-forward genuine block is preserved (don't clear on a 504).
        if _is_connectivity_error_text(exc) and not prior_genuine_block:
            state["recovery_first_unreachable_at"] = _iso_now()
            return _set_recovery_state(
                state,
                recovery_active=False,
                recovery_status="exchange_unreachable",
                recovery_requires_operator=False,
                recovery_summary=(
                    f"Startup recovery cannot reach Hyperliquid {network} "
                    f"(transient, auto-retrying — no action needed): {exc}"
                ),
                recovery_last_checked_at=_iso_now(),
            )
        return _set_recovery_state(
            state,
            recovery_active=True,
            recovery_status="error",
            recovery_requires_operator=True,
            recovery_summary=f"Startup recovery failed on Hyperliquid {network}: {exc}",
            recovery_last_checked_at=_iso_now(),
        )

    position_count = _count_exchange_positions(positions_payload)
    open_order_count = len(open_orders_payload) if isinstance(open_orders_payload, list) else 0
    discrepancy_count = len(recon.get("discrepancies", []) or []) if isinstance(recon, dict) else 0
    summary = (
        f"Startup recovery checked Hyperliquid {network}: "
        f"{position_count} exchange positions, {open_order_count} open orders, "
        f"{discrepancy_count} discrepancies."
    )
    adopted_count = int(recon.get("adopted_count", 0) or 0) if isinstance(recon, dict) else 0
    if adopted_count > 0:
        summary = f"{summary} Adopted {adopted_count} exchange position(s) into local management."

    if isinstance(account_snapshot, dict):
        _persist_exchange_account_snapshot(
            state,
            account_snapshot,
            testnet=testnet,
            synced_at=_iso_now(),
        )

    if (
        isinstance(recon, dict)
        and recon.get("error")
        and _is_reconcile_fetch_unavailable(recon)
        and not prior_genuine_block
    ):
        # Boot-time connectivity failure: soft, self-healing — see the except
        # branch above. Don't latch a hard halt across the restart, but DO keep a
        # carried-forward genuine block (prior_genuine_block) intact.
        state["recovery_first_unreachable_at"] = _iso_now()
        return _set_recovery_state(
            state,
            recovery_active=False,
            recovery_status="exchange_unreachable",
            recovery_requires_operator=False,
            recovery_position_count=position_count,
            recovery_summary=(
                f"{summary} Exchange unreachable (transient, auto-retrying — no action needed)."
            ),
            recovery_open_order_count=open_order_count,
            recovery_last_checked_at=_iso_now(),
        )

    if not isinstance(recon, dict) or recon.get("error"):
        return _set_recovery_state(
            state,
            recovery_active=True,
            recovery_status="error",
            recovery_position_count=position_count,
            recovery_discrepancy_count=max(discrepancy_count, 1),
            recovery_requires_operator=True,
            recovery_summary=f"{summary} Reconciliation failed; new entries remain blocked.",
            recovery_open_order_count=open_order_count,
            recovery_last_checked_at=_iso_now(),
        )

    active = discrepancy_count > 0
    return _set_recovery_state(
        state,
        recovery_active=active,
        recovery_status="blocked" if active else "ok",
        recovery_position_count=position_count,
        recovery_discrepancy_count=discrepancy_count,
        recovery_requires_operator=active,
        recovery_summary=(
            f"{summary} New entries remain blocked."
            if active
            else f"{summary} Exchange state is aligned for startup."
        ),
        recovery_open_order_count=open_order_count,
        recovery_last_checked_at=_iso_now(),
    )


def _update_recovery_state_from_reconcile(state: dict, recon: dict | None, *, source: str) -> None:
    if not isinstance(recon, dict):
        return

    network = str(state.get("recovery_network") or _network_label(_get_testnet()))
    checked_at = _iso_now()
    if recon.get("error"):
        # CR-2: distinguish a transient reconcile error from a real divergence.
        # Count consecutive errors; below the escalation threshold record a SOFT
        # 'error_transient' status that does NOT set recovery_active /
        # requires_operator (so new entries keep flowing). Only after N in a row
        # do we hard-block. _set_recovery_state carries prior recovery_active
        # forward, so a block from a genuine discrepancy is preserved.
        streak = int(state.get("recovery_error_streak", 0) or 0) + 1
        state["recovery_error_streak"] = streak

        # A pure CONNECTIVITY/read failure ("could not fetch exchange positions")
        # is NOT a divergence. Escalating it on a small consecutive-error COUNT is
        # exactly what hard-halted unattended trading on an ordinary testnet 504
        # burst. Keep it a SOFT, self-healing 'exchange_unreachable' state — do
        # NOT touch recovery_active/recovery_requires_operator (so any prior
        # genuine block is preserved, but a transient read failure never latches a
        # new one). New live opens stay fail-closed per-call via can_open Rule 0c,
        # and existing positions keep their on-exchange stops. Only after a
        # SUSTAINED outage (elapsed time, not count) do we raise a real hard halt
        # + operator alert.
        if _is_reconcile_fetch_unavailable(recon):
            first_unreachable_at = state.get("recovery_first_unreachable_at")
            if not first_unreachable_at:
                first_unreachable_at = checked_at
                state["recovery_first_unreachable_at"] = first_unreachable_at
            outage_seconds = _seconds_since(first_unreachable_at)
            if outage_seconds < _RECONCILE_OUTAGE_ESCALATE_SECONDS:
                _set_recovery_state(
                    state,
                    recovery_status="exchange_unreachable",
                    recovery_summary=(
                        f"{source.capitalize()} reconciliation cannot reach Hyperliquid "
                        f"{network} (transient, auto-retrying — no action needed): "
                        f"{recon.get('error')}"
                    ),
                    recovery_last_checked_at=checked_at,
                )
                return
            _set_recovery_state(
                state,
                recovery_active=True,
                recovery_status="error",
                recovery_requires_operator=True,
                recovery_summary=(
                    f"{source.capitalize()} reconciliation could not reach Hyperliquid "
                    f"{network} for {int(outage_seconds // 60)}m (sustained outage): "
                    f"{recon.get('error')}"
                ),
                recovery_last_checked_at=checked_at,
            )
            return

        # Non-connectivity error: keep the original CR-2 count-based escalation.
        if streak < _RECONCILE_ERROR_ESCALATE_AFTER:
            _set_recovery_state(
                state,
                recovery_status="error_transient",
                recovery_summary=(
                    f"{source.capitalize()} reconciliation error "
                    f"{streak}/{_RECONCILE_ERROR_ESCALATE_AFTER} on Hyperliquid {network} "
                    f"(transient, retrying): {recon.get('error')}"
                ),
                recovery_last_checked_at=checked_at,
            )
            return
        _set_recovery_state(
            state,
            recovery_active=True,
            recovery_status="error",
            recovery_requires_operator=True,
            recovery_summary=(
                f"{source.capitalize()} reconciliation failed {streak}x on Hyperliquid {network}: "
                f"{recon.get('error')}"
            ),
            recovery_last_checked_at=checked_at,
        )
        return

    # PARTIAL reconcile: at least one account/book could not be read (degraded),
    # but the reachable passes returned. If the reachable passes found real
    # discrepancies, fall through to the hard halt below. Otherwise we have NOT
    # confirmed the unreachable book(s) are aligned, so do NOT declare "all clear"
    # (which would clear a prior block and reset the outage timer) — hold a soft
    # 'exchange_unreachable' state and keep verifying. Dormant while direction
    # books are disabled (a single master pass either fully succeeds or errors).
    if recon.get("degraded") and not (recon.get("discrepancies") or []):
        first_unreachable_at = state.get("recovery_first_unreachable_at") or checked_at
        state["recovery_first_unreachable_at"] = first_unreachable_at
        unreachable = ", ".join(str(b) for b in (recon.get("unreachable_books") or [])) or "some accounts"
        _set_recovery_state(
            state,
            recovery_status="exchange_unreachable",
            recovery_summary=(
                f"{source.capitalize()} reconciliation could not read {unreachable} on "
                f"Hyperliquid {network} (partial; auto-retrying — no action needed)."
            ),
            recovery_last_checked_at=checked_at,
        )
        return

    # A clean reconcile (or a real discrepancy) clears the transient-error streak
    # and the sustained-outage timer.
    state["recovery_error_streak"] = 0
    state["recovery_first_unreachable_at"] = None
    discrepancy_count = len(recon.get("discrepancies", []) or [])
    position_count = int(recon.get("exchange_open", 0) or 0)
    if discrepancy_count > 0:
        _set_recovery_state(
            state,
            recovery_active=True,
            recovery_status="blocked",
            recovery_position_count=position_count,
            recovery_discrepancy_count=discrepancy_count,
            recovery_requires_operator=True,
            recovery_summary=(
                f"{source.capitalize()} reconciliation found {discrepancy_count} "
                f"discrepancies on Hyperliquid {network}; new entries remain blocked."
            ),
            recovery_last_checked_at=checked_at,
        )
        return

    next_status = "resolved" if state.get("recovery_active") else "ok"
    summary = (
        f"{source.capitalize()} reconciliation resolved exchange recovery on Hyperliquid {network}."
        if next_status == "resolved"
        else f"{source.capitalize()} reconciliation confirmed Hyperliquid {network} is aligned."
    )
    _set_recovery_state(
        state,
        recovery_active=False,
        recovery_status=next_status,
        recovery_position_count=position_count,
        recovery_discrepancy_count=0,
        recovery_requires_operator=False,
        recovery_summary=summary,
        recovery_last_checked_at=checked_at,
    )


def _equity_includes_master() -> bool:
    """Whether the master wallet counts toward the live equity basis (EQ-BASIS-1).

    Default False: with books enabled, only the direction sub-accounts hold
    at-risk capital."""
    try:
        settings = kv_get("forven:settings", {}) or {}
        if isinstance(settings, dict):
            return bool(settings.get("live_equity_include_master", False))
    except Exception:
        pass
    return False


def _book_aware_account_value(testnet: bool = True) -> dict | None:
    """Account value for the GLOBAL risk cycle, aggregated across direction books.

    With books disabled this is just the master wallet (unchanged). With books
    enabled, capital is split across funded sub-accounts, so the drawdown /
    daily-loss kill-switch sums accountValue + margin across every book
    sub-account — otherwise a loss bleeding a sub-account would never trip the
    global switch. The master wallet is EXCLUDED by default (EQ-BASIS-1: trades
    can only touch the books, so master reserve funds are not at-risk capital);
    live_equity_include_master opts it back in.

    ROBUST to transient reads: a single sub-account whose accountValue read
    momentarily returns 0 / fails (a degraded read, or funds mid-transfer) must
    NOT shrink the aggregate — that previously faked a large drawdown and tripped
    the kill-switch, flattening live positions on a glitch. So each account's
    last-known-good value is substituted on a non-positive/failed read. A genuine
    positive-but-lower balance still flows through (real losses are still caught).
    If an account has never read successfully (no last-known) and reads
    non-positive, the aggregate is UNRELIABLE → return None so the risk cycle
    skips this tick rather than acting on incomplete data.
    """
    global _LAST_BOOKS_ENABLED, _BOOKS_DISABLED_STREAK
    try:
        from forven.exchange import books

        if not books.books_enabled():
            # PNL-2: distinguish a transient books_enabled() flip from a genuine
            # operator disable. If books were recently confirmed ON, a brief
            # False read would crater equity to master-only (far below the
            # books-aggregate HWM) and trip the kill-switch on a glitch. Skip the
            # tick until books-off is confirmed for a few consecutive reads.
            _BOOKS_DISABLED_STREAK += 1
            if _LAST_BOOKS_ENABLED and _BOOKS_DISABLED_STREAK < _BOOKS_OFF_CONFIRM_TICKS:
                log.warning(
                    "book equity: books_enabled() read False after being enabled "
                    "(streak=%d/%d); skipping equity tick rather than cratering to "
                    "master-only",
                    _BOOKS_DISABLED_STREAK,
                    _BOOKS_OFF_CONFIRM_TICKS,
                )
                return None
            _LAST_BOOKS_ENABLED = False
            return get_account_value(testnet=testnet)

        _LAST_BOOKS_ENABLED = True
        _BOOKS_DISABLED_STREAK = 0

        # EQ-BASIS-1: with books enabled, live orders route ONLY to the direction
        # sub-accounts — the master wallet is reserve capital no trade can touch.
        # Counting it inflates every equity-denominated risk number (portfolio
        # budget, hard caps, drawdown/daily-loss base): a testnet master holding
        # ~$36k of mock funds swamped ~$600 of real trading capital in the books.
        # Default to books-only; live_equity_include_master opts the master back
        # in. The source label changes with the composition ("books_only" vs
        # "books_aggregate") so the risk engine's basis-change re-baseline
        # re-anchors the HWM / daily start instead of comparing across bases.
        include_master = _equity_includes_master()
        wallet_entries = ([("master", None)] if include_master else []) + list(books.active_book_addresses())
        wallets: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for label, addr in wallet_entries:
            key = (str(addr).strip().lower() if addr else "")
            if key in seen:
                continue
            seen.add(key)
            wallets.append((str(label or "master").strip().lower(), addr))
        source_label = "books_aggregate" if include_master else "books_only"

        total_val = total_margin = total_ntl = 0.0
        unreliable = False
        # KS-CACHE-LOG (2026-06-29 false kill-switch): a stale/inflated value in
        # _BOOK_EQUITY_CACHE silently poisoned the aggregate for hours because the
        # substitution path was debug-only. Track per-wallet provenance and emit a
        # LOUD, DURABLE (api.log) evidence line whenever any wallet falls back to
        # cache or a read fails — so a recurrence can be diagnosed live, BEFORE the
        # operator restarts (which wipes this in-memory cache and the evidence).
        substituted = False
        read_failed = False
        breakdown: list[str] = []
        # BOOK-BUDGET-1: per-wallet balances ride the snapshot (label -> equity)
        # so the per-book budget gate and the risk page can see each wallet's
        # capacity without extra exchange round-trips.
        books_equity: dict[str, float] = {}
        for label, addr in wallets:
            key = (str(addr).strip().lower() if addr else "__master__")
            kwargs = {"account_address": addr} if addr else {}
            raised = False
            err_reason = ""
            try:
                # EQ-BASIS-4: require a REAL exchange read for every wallet slot.
                # Without this, a transient client/breaker failure while the
                # execution mode is "paper" returns the daemon's OWN bookkeeping
                # (get_account_value's paper fallback: daily.current_equity →
                # daemon.account_equity) as if it were this wallet's balance — a
                # self-referential feedback loop that grew the aggregate by one
                # books-worth per tick (55 x $665.79 = the $36.6k phantom equity)
                # and can compound without bound. A failed read must RAISE and
                # ride the last-known-good / unreliable paths below instead.
                acc = get_account_value(testnet=testnet, require_connection=True, **kwargs)
                val = float(acc.get("accountValue", 0) or 0) if isinstance(acc, dict) else 0.0
            except Exception as exc:
                acc, val, raised = None, 0.0, True
                err_reason = f"{type(exc).__name__}: {exc}"
            if val > 0:
                _BOOK_EQUITY_CACHE[key] = val  # last-known-good
                total_val += val
                books_equity[label] = round(val, 2)
                breakdown.append(f"{key}=${val:,.2f}(live)")
                if isinstance(acc, dict):
                    total_margin += float(acc.get("totalMarginUsed", 0) or 0)
                    total_ntl += float(acc.get("totalNtlPos", 0) or 0)
            else:
                if raised:
                    read_failed = True
                cached = _BOOK_EQUITY_CACHE.get(key)
                if cached is not None and cached > 0:
                    # Had funds before, now reads 0/failed => transient glitch.
                    # Substitute last-known-good so it can't fake a drawdown.
                    total_val += cached
                    books_equity[label] = round(float(cached), 2)
                    substituted = True
                    breakdown.append(f"{key}=${cached:,.2f}(CACHED)")
                    log.warning(
                        "book equity: SUBSTITUTED cached $%.2f for %s (live read %s) — "
                        "stale cache can poison the aggregate",
                        cached, key, err_reason or "returned 0/non-positive",
                    )
                elif raised:
                    # Read ERRORED with no history => genuinely unknown; skip tick.
                    unreliable = True
                    breakdown.append(f"{key}=ERR({err_reason})")
                else:
                    # else: read returned 0 with no history => a legitimately EMPTY
                    # account (e.g. master drained, all capital in the sub-accounts).
                    # Count it as $0 — do NOT mark unreliable, or the daemon could
                    # never compute equity for a valid empty-master config.
                    breakdown.append(f"{key}=$0(empty)")
        if unreliable or total_val <= 0:
            log.warning(
                "book-aware equity read incomplete/unreliable this tick; skipping risk update [%s]",
                ", ".join(breakdown) or "no accounts",
            )
            return None
        if substituted or read_failed:
            log.warning(
                "book-aware equity used DEGRADED reads: total=$%.2f source=%s [%s]",
                total_val, source_label, ", ".join(breakdown),
            )
        return {
            "accountValue": total_val,
            "totalMarginUsed": total_margin,
            "totalNtlPos": total_ntl,
            "totalRawUsd": total_val,
            "source": source_label,
            "books": books_equity,
        }
    except Exception:
        # Hard failure: skip rather than fall back to a master-only value that
        # would itself look like a huge drawdown when books hold most of the capital.
        return None


def _check_liquidation_distances() -> dict:
    """H7: warn the operator when any OPEN position drifts toward liquidation.

    The only margin gate today is at OPEN time (80%); nothing watches an open
    position's distance to its liquidation price. This sweeps every account
    (master + direction sub-accounts) each tick and alerts (throttled via the
    notification cooldown) at warn / critical distance thresholds. Best-effort —
    never raises into the risk loop.

    Also returns a compact {"ASSET:direction": {...}} snapshot of the exchange's
    reported mark / unrealized PnL / entry for every open position, so the live
    session view can show the authoritative exchange P&L without issuing its own
    (WS-starving) exchange call — it reuses the positions this sweep already fetches.
    Returns {} on any failure.
    """
    snapshot: dict[str, dict] = {}
    try:
        from forven.db import kv_get
        from forven.exchange import books
        from forven.exchange.hyperliquid import get_all_mids, get_positions
        from forven.notifications import emit_notification

        s = kv_get("forven:settings", {}) or {}
        try:
            warn_pct = max(0.0, float(s.get("liq_distance_warn_pct", 15)) / 100.0)
            crit_pct = max(0.0, float(s.get("liq_distance_critical_pct", 7)) / 100.0)
        except Exception:
            warn_pct, crit_pct = 0.15, 0.07

        accounts: list[tuple[str | None, str]] = [(None, "master")]
        try:
            if books.books_enabled():
                for label, addr in books.active_book_addresses():
                    accounts.append((addr, label))
        except Exception:
            pass

        testnet = _get_testnet()
        seen: set[str] = set()
        for addr, label in accounts:
            key = str(addr or "").strip().lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                data = get_positions(testnet=testnet, **({"account_address": addr} if addr else {}))
                mids = get_all_mids(testnet=testnet)
            except Exception:
                continue
            for p in (data.get("positions", []) if isinstance(data, dict) else []):
                pos = p.get("position", p) if isinstance(p, dict) else {}
                coin = str(pos.get("coin") or "").strip().upper()
                try:
                    szi = float(pos.get("szi", 0) or 0)
                    liq = float(pos.get("liquidationPx") or 0)
                    mark = float(mids.get(coin, 0) or 0)
                except Exception:
                    continue
                if not coin or szi == 0 or mark <= 0:
                    continue
                # Record the exchange's authoritative position economics for the live
                # session view (keyed by asset+direction; last account wins on a dup).
                try:
                    direction = "long" if szi > 0 else "short"
                    snapshot[f"{coin}:{direction}"] = {
                        "asset": coin,
                        "direction": direction,
                        "mark_price": mark,
                        "entry_price": (float(pos.get("entryPx") or 0) or None),
                        "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
                        "size": abs(szi),
                    }
                except Exception:
                    pass
                if liq <= 0:
                    # Hyperliquid omits per-position liquidationPx for cross-margin
                    # positions (the liq price is account-wide). We can't compute a
                    # per-position distance for those — surface it instead of
                    # silently skipping, so cross-margin operators aren't lulled by
                    # the absence of alerts. (Default margin mode is isolated, which
                    # always carries a per-position liquidationPx.)
                    log.debug(
                        "Liquidation check: no per-position liqPx for %s on %s "
                        "(cross-margin?) — not monitored", coin, label,
                    )
                    continue
                dist = abs(mark - liq) / mark
                if dist <= crit_pct:
                    sev, lvl = "critical", "CRITICAL"
                elif dist <= warn_pct:
                    sev, lvl = "warning", "WARNING"
                else:
                    continue
                try:
                    emit_notification(
                        "risk_critical",
                        severity=sev,
                        source="daemon",
                        title=f"Liquidation risk: {coin} ({label})",
                        summary=f"{coin} is {dist:.1%} from liquidation on {label}",
                        body=(
                            f"{lvl}: {coin} position on '{label}' is {dist:.1%} from its liquidation "
                            f"price (mark {mark}, liqPx {liq}). Consider reducing or adding margin."
                        ),
                        dedupe_key=f"liq_distance:{label}:{coin}",
                    )
                except Exception:
                    pass
    except Exception as exc:
        log.debug("Liquidation-distance check failed: %s", exc)
    return snapshot


async def _run_risk_cycle() -> dict:
    """Run hard risk checks each tick; handle kill-switch and daily-loss halts."""
    snapshot = {"equity": None, "risk_check": None, "account": None}
    try:
        acct = await _to_thread_with_timeout(
            "daemon.get_account_value",
            RISK_ACCOUNT_TIMEOUT_SECONDS,
            _book_aware_account_value,
            testnet=_get_testnet(),
        )
        if not isinstance(acct, dict):
            return snapshot
        snapshot["account"] = dict(acct)

        equity = float(acct.get("accountValue", 0) or 0)
        if equity <= 0:
            return snapshot
        snapshot["equity"] = equity
        equity_source = acct.get("source", "exchange")

        risk_check = await _to_thread_with_timeout(
            "daemon.update_equity",
            RISK_UPDATE_TIMEOUT_SECONDS,
            update_equity,
            equity,
            equity_source,
        )
        if not isinstance(risk_check, dict):
            return snapshot

        snapshot["risk_check"] = dict(risk_check or {})
        # EQ-BASIS-2: a sample the risk validator REJECTED (implausible ceiling,
        # >100x jump) must not leak into the daemon_state mirrors — account_equity
        # and exchange_account feed the portfolio-budget denominator and the
        # dashboard directly, bypassing the validator. Keep the last good mirrors.
        if risk_check.get("rejected"):
            log.warning(
                "risk cycle: equity sample $%.2f REJECTED (%s) — not persisting to daemon_state",
                equity, risk_check.get("reject_reason"),
            )
            snapshot["equity"] = None
            snapshot["account"] = None
        # KS-1: drive the flatten off the PERSISTED kill_switch flag, not only the
        # one-shot transition action. The first trip flattens immediately; while
        # the switch stays active, re-sweep on a throttled cadence so a residual
        # position left by a missed/partial/failed/timed-out first flatten (or a
        # restart mid-flatten) is still closed — close_all_positions is a safe
        # no-op when already flat.
        kill_switch_active = bool(risk_check.get("kill_switch"))
        first_trip = risk_check.get("action") == "kill_switch"
        now_mono = time.time()
        do_flatten = first_trip or (
            kill_switch_active
            and now_mono - _LAST_KILL_REFLATTEN[0] >= KILL_REFLATTEN_INTERVAL_SECONDS
        )
        if kill_switch_active and do_flatten:
            _LAST_KILL_REFLATTEN[0] = now_mono
            log.critical(
                "KILL SWITCH — %s",
                "closing all positions" if first_trip else "re-sweeping for residual positions",
            )
            results = await _to_thread_with_timeout(
                "daemon.close_all_positions",
                RISK_CLOSE_TIMEOUT_SECONDS,
                close_all_positions,
            )
            if not isinstance(results, list):
                results = []
            # KS-3: report what ACTUALLY closed vs. failed, not just a count that
            # implies success — a partial/failed flatten must read as incomplete.
            closed_ok = [r for r in results if not (isinstance(r, dict) and r.get("error"))]
            failed = [r for r in results if isinstance(r, dict) and r.get("error")]
            if first_trip or results:
                try:
                    from forven.notifications import emit_notification

                    halt_line = (
                        f"Closed {len(closed_ok)}/{len(results)} positions."
                        + (f" {len(failed)} FAILED — will retry until flat." if failed else " Trading halted.")
                    )
                    await _to_thread_with_timeout(
                        "daemon.alert.kill_switch",
                        HEARTBEAT_SEND_TIMEOUT_SECONDS,
                        emit_notification,
                        "risk_critical",
                        severity="critical",
                        source="daemon",
                        title="Kill switch triggered" if first_trip else "Kill switch — residual position swept",
                        summary=f"Drawdown: {risk_check['drawdown_pct']:.1%} | {halt_line}",
                        body=(
                            "KILL SWITCH TRIGGERED\n"
                            f"Drawdown: {risk_check['drawdown_pct']:.1%}\n"
                            f"Equity: ${equity:,.2f} | HWM: ${risk_check['high_water_mark']:,.2f}\n"
                            f"{halt_line}"
                        ),
                        metadata={
                            "drawdown_pct": risk_check["drawdown_pct"],
                            "equity": equity,
                            "high_water_mark": risk_check["high_water_mark"],
                            "closed_positions": len(closed_ok),
                            "failed_positions": len(failed),
                            "first_trip": first_trip,
                        },
                    )
                except Exception:
                    pass
        elif risk_check.get("action") == "daily_halt":
            try:
                from forven.notifications import emit_notification

                await _to_thread_with_timeout(
                    "daemon.alert.daily_halt",
                    HEARTBEAT_SEND_TIMEOUT_SECONDS,
                    emit_notification,
                    "risk_critical",
                    severity="warn",
                    source="daemon",
                    title="Daily loss limit reached",
                    summary=f"Daily PnL: {risk_check['daily_pnl_pct']:.1%}",
                    body=(
                        "DAILY LOSS LIMIT\n"
                        f"Daily PnL: {risk_check['daily_pnl_pct']:.1%}\n"
                        "No new positions until tomorrow."
                    ),
                    metadata={"daily_pnl_pct": risk_check["daily_pnl_pct"]},
                )
            except Exception:
                pass
    except Exception as e:
        log.warning("Equity check failed: %s", e)
    return snapshot


def _run_mark_watcher(prices: dict[str, float]) -> list[str]:
    """Thread-side mark-watcher pass. Lazy import: forven.mark_watcher pulls the
    scanner module (heavy) — pay that once on the first armed check, not at
    daemon startup."""
    from forven.mark_watcher import check_mark_exits

    return check_mark_exits(prices)


async def _run_tick(state: dict, prices: dict[str, float], source: str, last_reconcile: list[float]) -> None:
    """One daemon tick: publish prices FIRST (fresh), then enforce risk, reconcile periodically."""
    now = time.time()

    # PRICE-FRESHNESS: publish the snapshot IMMEDIATELY, before the (possibly slow) risk cycle and
    # liquidation reads. publish_price_snapshot stamps updated_at at PUBLISH time, so doing it at
    # the END of a slow tick stamped a now-stale price as fresh — the consumers' 120s age gate is
    # blind to that (a ~6-min-old mark read as ~3s old, which filled a paper short ~$80 off the
    # candle). Publishing first keeps the value contemporaneous with its timestamp, and if a tick
    # DOES stall the timestamp simply ages (so the gate can finally see the staleness).
    snapshot = await _to_thread_with_timeout(
        "daemon.publish_price_snapshot",
        PRICE_SNAPSHOT_TIMEOUT_SECONDS,
        publish_price_snapshot,
        prices,
        source,
    )
    if not isinstance(snapshot, dict):
        snapshot = {"prices": prices}
    state["last_prices"] = dict(snapshot.get("prices") or prices)
    state["last_price_source"] = source
    state["last_tick_ts"] = now

    # Resting-order mark watcher: fill paper stop/TP the moment the level trades,
    # like a real exchange trigger order (the scanner's closed-bar monitors are the
    # catch-up backstop). Runs BEFORE the slow risk cycle so a touched level fills
    # within ~a tick of the touch, interval-gated to ~1s.
    if now - _LAST_MARK_WATCH[0] >= MARK_WATCH_MIN_INTERVAL_SECONDS:
        _LAST_MARK_WATCH[0] = now
        try:
            mark_actions = await _to_thread_with_timeout(
                "daemon.mark_watcher",
                MARK_WATCH_TIMEOUT_SECONDS,
                _run_mark_watcher,
                prices,
            )
            if mark_actions:
                log.info("Mark watcher: %s", "; ".join(mark_actions))
                state["last_mark_watch_actions"] = list(mark_actions)
                state["last_mark_watch_at"] = _iso_now()
        except Exception as e:
            log.warning("Mark watcher tick failed: %s", e)

    risk_snapshot = await _run_risk_cycle()

    # H7: per-position liquidation-distance monitoring. Interval-gated (NOT every
    # tick): _run_tick fires per price message with no min-interval, and each
    # check issues a get_positions REST read per book account. A position's
    # distance to liquidation does not change meaningfully sub-minute, and the
    # alert itself is cooldown-throttled, so a 60s cadence bounds REST load on
    # the shared account breaker while still catching a developing liquidation.
    if now - _LAST_LIQ_CHECK[0] >= LIQ_CHECK_INTERVAL_SECONDS:
        _LAST_LIQ_CHECK[0] = now
        try:
            positions_snapshot = await _to_thread_with_timeout(
                "daemon.liquidation_check",
                RISK_ACCOUNT_TIMEOUT_SECONDS,
                _check_liquidation_distances,
            )
            # Cache the exchange's reported mark / unrealized PnL per position so the
            # live session view shows the authoritative exchange P&L (refreshed on
            # this 60s cadence) without its own exchange round-trip.
            if isinstance(positions_snapshot, dict):
                state["exchange_positions"] = positions_snapshot
                state["exchange_positions_synced_at"] = _iso_now()
        except Exception:
            pass

    state["tick_count"] = int(state.get("tick_count", 0)) + 1
    # Backward compatibility with legacy UI that still reads scan_count.
    state["scan_count"] = state["tick_count"]
    state["last_scan"] = _iso_now()

    if isinstance(risk_snapshot, dict):
        equity = risk_snapshot.get("equity")
        if isinstance(equity, (int, float)) and float(equity) > 0:
            state["account_equity"] = float(equity)
        account_snapshot = risk_snapshot.get("account")
        if isinstance(account_snapshot, dict):
            _persist_exchange_account_snapshot(
                state,
                account_snapshot,
                testnet=_get_testnet(),
                synced_at=_iso_now(),
            )
        risk_check = risk_snapshot.get("risk_check")
        if isinstance(risk_check, dict):
            state["risk"] = {
                "drawdown_pct": float(risk_check.get("drawdown_pct", 0.0) or 0.0),
                "daily_pnl_pct": float(risk_check.get("daily_pnl_pct", 0.0) or 0.0),
                "high_water_mark": float(risk_check.get("high_water_mark", 0.0) or 0.0),
                "kill_switch": bool(risk_check.get("kill_switch", False)),
                "daily_halt": bool(risk_check.get("daily_halt", False)),
            }

    if now - last_reconcile[0] > 600:
        state["last_reconcile_attempt"] = _iso_now()
        try:
            mode = str(get_execution_mode() or "paper").strip().lower()
            creds_available, creds_error = _exchange_credentials_status()
            if not creds_available and mode == "paper":
                state["last_reconcile"] = _iso_now()
                state["last_reconcile_status"] = "skipped_no_credentials"
                state["last_reconcile_error"] = None
                state["reconciliation_issues"] = 0
                _set_recovery_state(
                    state,
                    recovery_active=False,
                    recovery_status="skipped_no_credentials",
                    recovery_requires_operator=False,
                    recovery_summary=(
                        f"Periodic reconciliation skipped on Hyperliquid "
                        f"{_network_label(_get_testnet())}: credentials not configured."
                    ),
                    recovery_last_checked_at=state["last_reconcile"],
                )
            elif not creds_available:
                recon = {"error": creds_error or "missing Hyperliquid credentials"}
                state["last_reconcile"] = _iso_now()
                state["last_reconcile_status"] = "error"
                state["last_reconcile_error"] = str(recon["error"])
                _update_recovery_state_from_reconcile(state, recon, source="periodic")
            else:
                recon = await _to_thread_with_timeout(
                    "daemon.reconcile_all_books",
                    _reconcile_timeout_seconds(),
                    reconcile_all_books,
                    _get_testnet(),
                )
                if not isinstance(recon, dict):
                    recon = {"error": "Reconciliation timed out or returned no data"}
                _update_recovery_state_from_reconcile(state, recon, source="periodic")

                if isinstance(recon, dict) and recon.get("discrepancies"):
                    state["last_reconcile"] = _iso_now()
                    state["last_reconcile_status"] = "issues"
                    state["last_reconcile_error"] = None
                    state["reconciliation_issues"] = len(recon["discrepancies"])
                    try:
                        from forven.bot import send_sync

                        await _to_thread_with_timeout(
                            "daemon.alert.reconciliation",
                            HEARTBEAT_SEND_TIMEOUT_SECONDS,
                            send_sync,
                            "alerts",
                            "RECONCILIATION WARNING\n"
                            + f"{len(recon['discrepancies'])} discrepancies found:\n"
                            + "\n".join(f"- {d['details']}" for d in recon["discrepancies"][:5]),
                        )
                    except Exception:
                        pass
                elif isinstance(recon, dict) and recon.get("error"):
                    state["last_reconcile"] = _iso_now()
                    state["last_reconcile_status"] = "error"
                    state["last_reconcile_error"] = str(recon.get("error"))
                    state["reconciliation_issues"] = int(
                        state.get("recovery_discrepancy_count", 0) or 0
                    )
                elif isinstance(recon, dict):
                    state["last_reconcile"] = _iso_now()
                    state["last_reconcile_status"] = "ok"
                    state["last_reconcile_error"] = None
                    state["reconciliation_issues"] = 0
        except Exception as e:
            state["last_reconcile"] = _iso_now()
            state["last_reconcile_status"] = "error"
            state["last_reconcile_error"] = str(e)
            _update_recovery_state_from_reconcile(state, {"error": str(e)}, source="periodic")
            log.debug("Reconciliation skipped: %s", e)
        # CR-2: after a transient (not-yet-escalated) reconcile error, schedule
        # the next attempt on the short retry cadence instead of the full 600s so
        # an isolated blip clears quickly rather than freezing the gate for 10min.
        if state.get("last_reconcile_status") == "error" and not state.get("recovery_active"):
            last_reconcile[0] = now - 600 + _RECONCILE_ERROR_RETRY_SECONDS
        else:
            last_reconcile[0] = now

    await asyncio.to_thread(_persist_daemon_state, state)


async def _run_heartbeat_loop(state: dict, prices: dict, last_heartbeat: float) -> float:
    now = time.time()
    if now - last_heartbeat >= HEARTBEAT_INTERVAL:
        await _to_thread_with_timeout(
            "daemon.send_heartbeat",
            HEARTBEAT_SEND_TIMEOUT_SECONDS,
            send_heartbeat,
            state,
            prices,
        )
        state["last_heartbeat"] = now
        return now
    return last_heartbeat


async def _refresh_candle_cache(state: dict) -> None:
    """Refresh shared OHLCV cache for scanner/evaluation workers."""
    updated_assets: dict[str, int] = {}
    for coin in _active_coins():
        try:
            df = await _to_thread_with_timeout(
                f"daemon.fetch_market_candles.{coin}",
                CANDLE_FETCH_TIMEOUT_SECONDS,
                fetch_market_candles,  # source-aware: Binance by default
                coin,
                bars=CANDLE_CACHE_BARS,
                interval="1h",
            )
            if df is None:
                continue
            rows = dataframe_to_ohlcv_rows(df, max_rows=CANDLE_CACHE_BARS)
            publish_result = await _to_thread_with_timeout(
                f"daemon.publish_candle_snapshot.{coin}",
                CANDLE_FETCH_TIMEOUT_SECONDS,
                publish_candle_snapshot,
                coin,
                rows,
                "daemon",
                interval="1h",
                max_rows=CANDLE_CACHE_BARS,
            )
            if publish_result is None:
                continue
            updated_assets[coin] = len(rows)
        except Exception as exc:
            log.debug("Candle cache refresh failed for %s: %s", coin, exc)

    if updated_assets:
        state["last_candle_sync"] = _iso_now()
        state["candle_cache_assets"] = sorted(updated_assets.keys())
        state["candle_cache_rows"] = updated_assets
        state["candle_cache_interval_seconds"] = _runtime_candle_refresh_interval()
        await asyncio.to_thread(_persist_daemon_state, state)


async def _price_consumer(price_queue: asyncio.Queue, state: dict):
    """Consume market ticks and run data/risk tick processing."""
    last_reconcile = [0.0]
    last_heartbeat = 0.0

    while not shutdown.is_set():
        try:
            try:
                source, prices = await asyncio.wait_for(price_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            clean_prices = normalize_prices(prices, allowed_assets=_active_coins())
            if not clean_prices:
                continue

            await _run_tick(state, clean_prices, source, last_reconcile)
            last_heartbeat = await _run_heartbeat_loop(state, clean_prices, last_heartbeat)
        except Exception as e:
            log.error("Unhandled exception in price consumer loop: %s", e, exc_info=True)
            await asyncio.sleep(1)


async def async_market_loop(state: dict):
    """Async event-driven daemon loop (data ingestion + risk only)."""
    # H-R2: register signal handlers on the running loop so POSIX SIGTERM/SIGINT
    # wake the loop immediately instead of only being noticed on the next
    # `await shutdown.is_set()` check. No-op on Windows (add_signal_handler is
    # not implemented there) — we rely on the signal.signal handler in run().
    try:
        loop = asyncio.get_running_loop()
        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, shutdown.set)
            except (NotImplementedError, RuntimeError, ValueError):
                # Windows / non-main-thread: fall back to signal.signal from run()
                pass
    except Exception:
        log.debug("Could not install asyncio signal handlers", exc_info=True)

    price_queue: asyncio.Queue = asyncio.Queue(maxsize=100)

    async def enqueue_price(source: str, prices: dict):
        if price_queue.full():
            try:
                price_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        # Defensive: the drain-then-put pattern above should make put()
        # non-blocking, but an unguarded await here would still cause the
        # feeder callback to hang indefinitely if the consumer task were
        # cancelled or stopped draining. Cap the wait so a stuck consumer
        # cannot freeze the WebSocket feed.
        try:
            await asyncio.wait_for(price_queue.put((source, prices)), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("enqueue_price timed out (source=%s); dropping tick", source)

    async def on_price(prices: dict):
        await enqueue_price("ws", prices)

    consumer: asyncio.Task | None = None
    feeder: asyncio.Task | None = None

    async def _start_market_workers() -> tuple[asyncio.Task, asyncio.Task]:
        # Source-aware price feed: Binance (the lead exchange) by default so paper
        # marking/display uses REAL Binance prices, not HyperLiquid testnet.
        if resolve_market_data_source() == "binance":
            feed = BinancePriceFeed(coins=_active_coins, on_price=on_price)
        else:
            feed = HyperLiquidFeed(coins=_active_coins, on_price=on_price)
        return (
            spawn(_price_consumer(price_queue, state), name="daemon-price-consumer"),
            spawn(feed.start(), name="daemon-feed"),
        )

    async def _stop_market_workers() -> None:
        nonlocal consumer, feeder
        active = [task for task in (consumer, feeder) if task is not None]
        if not active:
            return
        for task in active:
            task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*active, return_exceptions=True),
                timeout=SHUTDOWN_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            log.warning(
                "Daemon market workers shutdown timed out after %.1fs",
                SHUTDOWN_GRACE_SECONDS,
            )
        consumer = None
        feeder = None

    last_fallback_poll = 0.0
    last_candle_refresh = 0.0
    try:
        while not shutdown.is_set():
            try:
                await asyncio.sleep(1)
                if not autonomous_runtime_allowed():
                    if consumer is not None or feeder is not None:
                        await _stop_market_workers()
                    state["market_loop_status"] = "paused_manual"
                    state["last_price_source"] = "manual_pause"
                    await asyncio.to_thread(_persist_daemon_state, state)
                    continue

                if consumer is None or feeder is None:
                    consumer, feeder = await _start_market_workers()
                    state["market_loop_status"] = "active"
                    await asyncio.to_thread(_persist_daemon_state, state)

                now = time.time()

                # Fallback polling when WS feed is quiet.
                last_tick_ts = float(state.get("last_tick_ts", 0) or 0)
                if (
                    now - last_tick_ts >= PRICE_FALLBACK_POLL_INTERVAL
                    and now - last_fallback_poll >= PRICE_FALLBACK_POLL_INTERVAL
                ):
                    try:
                        if resolve_market_data_source() == "binance":
                            mids = await _to_thread_with_timeout(
                                "daemon.fetch_binance_prices.fallback",
                                FALLBACK_MIDS_TIMEOUT_SECONDS,
                                fetch_binance_prices,
                                _active_coins(),
                            )
                        else:
                            mids = await _to_thread_with_timeout(
                                "daemon.get_all_mids.fallback",
                                FALLBACK_MIDS_TIMEOUT_SECONDS,
                                get_all_mids,
                                _get_testnet(),
                            )
                        if isinstance(mids, dict):
                            await enqueue_price("poll", mids)
                    except Exception as e:
                        log.debug("Fallback price poll failed: %s", e)
                    last_fallback_poll = now

                refresh_interval = _runtime_candle_refresh_interval()
                state["candle_cache_interval_seconds"] = refresh_interval
                if now - last_candle_refresh >= refresh_interval:
                    await _refresh_candle_cache(state)
                    last_candle_refresh = now

                if now - float(state.get("last_heartbeat", 0) or 0) > HEARTBEAT_INTERVAL * 3:
                    try:
                        state["heartbeat_gap"] = int(now)
                        await asyncio.to_thread(_persist_daemon_state, state)
                    except Exception:
                        pass
            except Exception as e:
                log.error("Unhandled exception in async_market_loop: %s", e, exc_info=True)
                await asyncio.sleep(1)
    finally:
        await _stop_market_workers()


def market_scan_loop(state: dict):
    """Compatibility shim for direct synchronous consumers."""
    asyncio.run(async_market_loop(state))


def _daemon_startup_bookkeeping(install_signal_handlers: bool) -> dict | None:
    """Shared prelude for both the sync `run()` entry point and the async
    in-process variant used by `forven.api`. Returns the initial state dict
    or None if the lock couldn't be acquired."""
    if not _acquire_daemon_lock():
        log.warning("Another daemon instance is already running; skipping duplicate start.")
        try:
            log_activity("info", "daemon", "Skipped daemon start (lock already held)")
        except Exception:
            pass
        return None

    swept = _sweep_orphan_multiprocessing_workers()
    if swept:
        log.warning("Reaped %d orphan multiprocessing worker(s) from prior runs", swept)
        try:
            log_activity("warning", "daemon", f"Reaped {swept} orphan multiprocessing worker(s) from prior runs")
        except Exception:
            pass

    init_db()
    shutdown.clear()

    if install_signal_handlers:
        # H-R2: on POSIX, loop.add_signal_handler lets the event loop wake up
        # immediately on signal delivery. On Windows, signal.signal only handles
        # SIGINT (Ctrl+C) and runs on the main thread.
        def _handle_shutdown_signal(signum, frame):
            try:
                shutdown.set()
            except Exception:
                pass

        try:
            signal.signal(signal.SIGINT, _handle_shutdown_signal)
        except (ValueError, OSError):
            # ValueError raised if called from non-main thread (e.g. when the
            # API hosts us as a task alongside uvicorn's own signal handlers).
            pass
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, _handle_shutdown_signal)
            except (ValueError, OSError):
                pass

    log.info("=" * 50)
    log.info("FORVEN DAEMON v2.0 — DATA/RISK LOOP STARTING")
    log.info("=" * 50)

    state = {
        "started_at": _iso_now(),
        "start_ts": time.time(),
        "pid": os.getpid(),
        "tick_count": 0,
        "scan_count": 0,  # legacy compatibility
        "running": True,
        "scanner_mode": "external_scheduler",
        "candle_cache_interval_seconds": _runtime_candle_refresh_interval(),
    }
    runtime_code = compute_runtime_code_fingerprint()
    state["runtime_code_fingerprint"] = runtime_code.get("fingerprint")
    state["runtime_code_files"] = list(runtime_code.get("files") or [])
    state["runtime_code_captured_at"] = runtime_code.get("generated_at")
    state.update(_default_recovery_state())
    # CR-1: do NOT persist recovery_active=False before the preflight runs.
    # The scanner runs on a separate thread and reads this exact KV; clearing the
    # gate to idle here opens it for the multi-second reconcile window — precisely
    # when a prior crash may have left a genuine orphaned position that should
    # keep entries blocked. Carry a prior persisted block forward and fail CLOSED
    # ('checking') until the preflight confirms the exchange is aligned.
    startup_prior_genuine_block = False
    try:
        prior = kv_get("daemon_state", {}) or {}
        if isinstance(prior, dict) and (
            bool(prior.get("recovery_active"))
            or str(prior.get("recovery_status") or "").strip().lower() in {"blocked", "error", "checking"}
        ):
            for key in _default_recovery_state():
                if key in prior:
                    state[key] = prior[key]
        # A carried-forward GENUINE block (real divergence / sustained outage) must
        # survive even if the boot preflight then throws a transient connectivity
        # error — see Finding A. 'checking' alone is not a genuine block.
        if isinstance(prior, dict):
            startup_prior_genuine_block = bool(prior.get("recovery_active")) and (
                str(prior.get("recovery_status") or "").strip().lower() in {"blocked", "error"}
                or int(prior.get("recovery_discrepancy_count", 0) or 0) > 0
            )
    except Exception:
        pass
    _set_recovery_state(
        state,
        recovery_active=True,
        recovery_status="checking",
        recovery_requires_operator=bool(state.get("recovery_requires_operator", False)),
        recovery_summary="Startup exchange recovery check in progress; new entries blocked until reconciled.",
    )
    _persist_daemon_state(state)

    # KS-1: if a prior run left the kill-switch tripped, re-flatten on boot before
    # resuming. The in-loop flatten is one-shot and may have been interrupted by
    # the crash/restart, leaving live positions open and unmanaged.
    try:
        if bool(get_risk_status().get("kill_switch_active")):
            log.critical("KILL SWITCH active at startup — flattening residual positions before resume")
            _res = close_all_positions()
            _ok = len([r for r in _res if not (isinstance(r, dict) and r.get("error"))]) if isinstance(_res, list) else 0
            log_activity("critical", "daemon", f"Startup kill-switch re-flatten: closed {_ok} residual position(s)")
    except Exception as exc:
        log.warning("Startup kill-switch re-flatten failed: %s", exc)

    try:
        recovery = run_startup_recovery_preflight(state)
        state["last_reconcile"] = _iso_now()
        state["last_reconcile_status"] = str(recovery.get("recovery_status") or "unknown")
        state["last_reconcile_error"] = None if state["last_reconcile_status"] not in {"error"} else str(
            recovery.get("recovery_summary") or "startup recovery failed"
        )
        state["reconciliation_issues"] = int(recovery.get("recovery_discrepancy_count", 0) or 0)
        _persist_daemon_state(state)
    except Exception as exc:
        log.warning("Daemon startup recovery preflight failed: %s", exc)
        # CR-1: a preflight that throws must leave the gate CLOSED, not the
        # pre-cleared idle state — an unverified exchange is treated as unsafe.
        # EXCEPTION: a transient connectivity error is self-healing — it stays a
        # soft 'exchange_unreachable' state (opens still fail-closed per-call by
        # can_open Rule 0c) instead of a latched operator-required halt. But a
        # carried-forward genuine block (Finding A) is preserved as a hard halt.
        if _is_connectivity_error_text(exc) and not startup_prior_genuine_block:
            state["recovery_first_unreachable_at"] = _iso_now()
            _set_recovery_state(
                state,
                recovery_active=False,
                recovery_status="exchange_unreachable",
                recovery_requires_operator=False,
                recovery_summary=(
                    f"Startup exchange recovery cannot reach Hyperliquid "
                    f"(transient, auto-retrying — no action needed): {exc}"
                ),
            )
        else:
            _set_recovery_state(
                state,
                recovery_active=True,
                recovery_status="error",
                recovery_requires_operator=True,
                recovery_summary=f"Startup exchange recovery check errored: {exc}",
            )
        state["last_reconcile"] = _iso_now()
        state["last_reconcile_status"] = "error"
        state["last_reconcile_error"] = str(exc)
        _persist_daemon_state(state)

    log_activity("info", "daemon", "Daemon started (data/risk mode)")

    try:
        from forven.notifications import emit_notification

        emit_notification(
            "system_recovered",
            source="daemon",
            title="Forven daemon online",
            summary="Mode: data+risk only (scanner runs via scheduler worker)",
            body="Daemon startup complete. Heartbeats remain visible in /ops and logs.",
            metadata={"component": "daemon"},
            dedupe_key="daemon-online",
        )
    except Exception:
        pass

    return state


def _daemon_shutdown_bookkeeping(state: dict | None) -> None:
    if state is not None:
        try:
            state["running"] = False
            state["stopped_at"] = _iso_now()
            _persist_daemon_state(state)
        except Exception:
            pass
        log.info("Daemon shutdown complete.")
    try:
        _release_daemon_lock()
    except Exception:
        pass


class DaemonLoopDeclined:
    """Sentinel returned by :func:`run_in_loop` when the singleton daemon lock
    is already held by another instance (e.g. a standalone ``forven daemon
    start`` process running alongside the API).

    The ``stop_supervision`` flag tells the API's ``_supervise_background_loop``
    that this is a *stable, expected* terminal condition -- the external daemon
    already owns the data/risk loop -- so the in-process loop must NOT be
    restarted. Without it, the supervisor treated this clean early-return as an
    unexpected exit and hot-restarted every ~5s forever, spamming
    "Skipped daemon start (lock already held)" into activity_log (observed:
    tens of thousands of rows) and re-attempting the file lock each cycle.
    """

    stop_supervision = True

    def __init__(self, reason: str = "daemon lock held by another instance") -> None:
        self.reason = reason

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.reason


async def run_in_loop() -> "DaemonLoopDeclined | None":
    """Async daemon entry for hosts that already own an event loop.

    Used by the API process (uvicorn) so a single Tauri-launched backend can
    serve HTTP AND drive the data/risk loop without requiring a separate
    `forven daemon start` process. The file lock still prevents duplicate
    daemons if an external one happens to be running.

    Returns a :class:`DaemonLoopDeclined` sentinel (truthy ``stop_supervision``)
    when the lock is already held, so the supervisor stops instead of
    hot-restarting; returns ``None`` if the data/risk loop itself exits.
    """
    state = _daemon_startup_bookkeeping(install_signal_handlers=False)
    if state is None:
        # Another instance owns the singleton lock -- a stable, expected no-op,
        # not a crash. Signal the supervisor to stand down rather than respawn
        # us every few seconds.
        return DaemonLoopDeclined()
    try:
        await async_market_loop(state)
    finally:
        _daemon_shutdown_bookkeeping(state)
    return None


def run():
    """Main daemon entry point (standalone process)."""
    state = _daemon_startup_bookkeeping(install_signal_handlers=True)
    if state is None:
        return
    # Arm fail-closed spend enforcement in this standalone process too.
    from forven.model_selection import ensure_enforcement_armed
    ensure_enforcement_armed()
    try:
        asyncio.run(async_market_loop(state))
    finally:
        _daemon_shutdown_bookkeeping(state)


if __name__ == "__main__":
    run()
