"""
Forven Process Watchdog
=======================
Monitors the bot and daemon processes. Restarts the bot if the scheduler
has stalled (no jobs have run for >5 minutes).

Usage:
    python scripts/watchdog.py          # run once (for Task Scheduler)
    python scripts/watchdog.py --loop   # run continuously every 60s

The watchdog is intentionally simple and self-contained to avoid depending
on any Forven internals that might be broken.
"""

import os
import sys
import time
import signal
import subprocess
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FORVEN_HOME = Path(os.environ.get("FORVEN_HOME", os.path.expanduser("~/.forven")))
BOT_LOCK = FORVEN_HOME / "bot.lock"
DAEMON_LOCK = FORVEN_HOME / "daemon.lock"
DB_PATH = FORVEN_HOME / "forven.db"
LOG_PATH = FORVEN_HOME / "logs" / "watchdog.log"
POLL_INTERVAL = 60  # seconds between checks
SCHEDULER_STALE_THRESHOLD = 300  # 5 minutes — scheduler must tick within this window
BOT_START_CMD = [sys.executable, "-m", "forven", "bot", "start"]
BOT_STARTUP_GRACE_SECONDS = 180  # allow a fresh gateway time to connect and tick

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("watchdog")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_pid(lock_path: Path) -> int | None:
    """Read PID from a lock file. Returns None if missing or invalid."""
    try:
        return int(lock_path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running (Windows)."""
    try:
        # On Windows, signal 0 doesn't work the same way.
        # Use tasklist to check.
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _kill_pid(pid: int) -> bool:
    """Kill a process by PID."""
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(2)
        if _is_pid_alive(pid):
            os.kill(pid, signal.SIGBREAK)  # Windows forceful
            time.sleep(2)
        return not _is_pid_alive(pid)
    except Exception as e:
        log.warning("Failed to kill PID %d: %s", pid, e)
        return False


def _get_latest_scheduler_run() -> datetime | None:
    """Query the DB for the most recent scheduler progress timestamp."""
    if not DB_PATH.exists():
        return None
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=5)
        conn.row_factory = sqlite3.Row
        job_row = conn.execute(
            "SELECT MAX(last_run_at) as latest FROM scheduler_jobs WHERE enabled = 1"
        ).fetchone()
        kv_row = conn.execute(
            "SELECT value FROM kv WHERE key = 'scheduler:last_successful_tick'"
        ).fetchone()
        conn.close()
        candidates: list[datetime] = []
        for ts in (
            job_row["latest"] if job_row and job_row["latest"] else None,
            kv_row["value"] if kv_row and kv_row["value"] else None,
        ):
            if not ts:
                continue
            dt = datetime.fromisoformat(str(ts))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            candidates.append(dt)
        if candidates:
            return max(candidates)
    except Exception as e:
        log.warning("Failed to query scheduler state: %s", e)
    return None


def _get_bot_lock_age_seconds() -> float | None:
    """Return the approximate age of the current bot lock file."""
    try:
        return max(0.0, time.time() - BOT_LOCK.stat().st_mtime)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.debug("Could not read bot lock age: %s", e)
        return None


def _start_bot() -> int | None:
    """Start the bot process in the background. Returns the new PID."""
    try:
        # CONSOLE-2: CREATE_NO_WINDOW, not DETACHED_PROCESS — a detached child has
        # no console at all, so every console-subsystem process IT spawns pops a
        # visible console window (and Windows ignores CREATE_NO_WINDOW when
        # DETACHED_PROCESS is set — see bot_factory.manager). With NO_WINDOW the
        # child owns its own hidden console: still isolated from this console's
        # Ctrl+C/close, but its children inherit the hidden console silently.
        proc = subprocess.Popen(
            BOT_START_CMD,
            cwd=str(Path(__file__).resolve().parent.parent),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info("Started bot process with PID %d", proc.pid)
        return proc.pid
    except Exception as e:
        log.error("Failed to start bot: %s", e)
        return None


# ---------------------------------------------------------------------------
# Main check
# ---------------------------------------------------------------------------

def run_check() -> None:
    """Run a single watchdog check cycle."""
    now = datetime.now(timezone.utc)

    # --- Daemon check (info only — don't restart daemon automatically) ---
    daemon_pid = _read_pid(DAEMON_LOCK)
    if daemon_pid:
        if _is_pid_alive(daemon_pid):
            log.debug("Daemon PID %d is alive", daemon_pid)
        else:
            log.warning("Daemon PID %d from lock file is DEAD", daemon_pid)

    # --- Bot check ---
    bot_pid = _read_pid(BOT_LOCK)
    if bot_pid and not _is_pid_alive(bot_pid):
        log.warning("Bot PID %d from lock file is DEAD — will restart", bot_pid)
        try:
            BOT_LOCK.unlink(missing_ok=True)
        except Exception:
            pass
        _start_bot()
        return

    # --- Scheduler stale check ---
    latest_run = _get_latest_scheduler_run()
    if latest_run is None:
        log.warning("Could not determine latest scheduler run — skipping stale check")
        return

    age = (now - latest_run).total_seconds()
    lock_age = _get_bot_lock_age_seconds()
    if (
        age > SCHEDULER_STALE_THRESHOLD
        and bot_pid
        and _is_pid_alive(bot_pid)
        and lock_age is not None
        and lock_age < BOT_STARTUP_GRACE_SECONDS
    ):
        log.info(
            "Scheduler looks stale (%.0fs) but bot PID %s started %.0fs ago - within startup grace window, skipping restart.",
            age,
            bot_pid,
            lock_age,
        )
        return
    if age > SCHEDULER_STALE_THRESHOLD:
        log.critical(
            "Scheduler is STALE: last job ran %.0fs ago (threshold: %ds). "
            "Killing bot PID %s and restarting.",
            age, SCHEDULER_STALE_THRESHOLD, bot_pid,
        )
        if bot_pid and _is_pid_alive(bot_pid):
            killed = _kill_pid(bot_pid)
            if killed:
                log.info("Killed stale bot PID %d", bot_pid)
            else:
                log.error("Could not kill stale bot PID %d — manual intervention needed", bot_pid)
                return
        try:
            BOT_LOCK.unlink(missing_ok=True)
        except Exception:
            pass
        _start_bot()
    else:
        log.debug("Scheduler healthy: last job ran %.0fs ago", age)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if "--loop" in sys.argv:
        log.info("Watchdog starting in loop mode (interval=%ds)", POLL_INTERVAL)
        while True:
            try:
                run_check()
            except Exception as e:
                log.error("Watchdog check failed: %s", e)
            time.sleep(POLL_INTERVAL)
    else:
        log.info("Watchdog running single check")
        run_check()


if __name__ == "__main__":
    main()
