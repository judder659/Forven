"""In-app self-update via git.

Forven is run from a git checkout (users clone the repo and launch it with
``start_all``). This module lets the running app see when the tracked branch on
the remote has moved ahead and fast-forward the local checkout to it — the
"check for updates" button in Settings and the startup "update available"
banner both call into here.

Applying an update is just ``git fetch`` + ``git pull --ff-only``. The actual
process restart is handled by the caller (the updates router) by reusing the
same self-exit primitive as ``/api/shutdown``: the backend signals uvicorn to
stop and the ``start_all`` watchdog relaunches it on the freshly pulled code.

Design choices, all on the side of "never surprise the operator":
- We track a fixed remote/branch (default ``origin``/``main``) — configurable
  via ``FORVEN_UPDATE_REMOTE`` / ``FORVEN_UPDATE_BRANCH``.
- We only ever fast-forward. A dirty working tree, local commits the remote
  doesn't have, or sitting on a different branch all *block* the apply with a
  clear reason rather than rewriting the operator's state.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import forven

log = logging.getLogger("forven.self_update")

# Defense in depth: even though every git call uses argv (shell=False), reject
# operator-supplied remote/branch values that stray outside the safe charset.
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_GIT_REMOTE_RE = re.compile(r"^[A-Za-z0-9._\-]+$")

# Serialize apply so two concurrent clicks can't race two pulls.
_APPLY_LOCK = threading.Lock()

_FETCH_TIMEOUT_S = 60
_GIT_TIMEOUT_S = 30


def _repo_root() -> Path:
    # self_update.py lives at <repo>/forven/self_update.py
    return Path(__file__).resolve().parents[1]


def _get_env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _target_remote() -> str:
    value = _get_env("FORVEN_UPDATE_REMOTE", "origin")
    if not _GIT_REMOTE_RE.match(value):
        raise RuntimeError(f"Invalid FORVEN_UPDATE_REMOTE: {value!r}")
    return value


def _target_branch() -> str:
    value = _get_env("FORVEN_UPDATE_BRANCH", "main")
    if not _GIT_REF_RE.match(value):
        raise RuntimeError(f"Invalid FORVEN_UPDATE_BRANCH: {value!r}")
    return value


def _is_git_checkout() -> bool:
    return (_repo_root() / ".git").exists()


def _git(*args: str, timeout: int = _GIT_TIMEOUT_S, check: bool = True) -> subprocess.CompletedProcess:
    repo = str(_repo_root())
    try:
        proc = subprocess.run(
            ["git", "-C", repo, *args],
            text=True,
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # git not installed / not on PATH
        raise RuntimeError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git {args[0] if args else ''} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed ({proc.returncode}): {detail[:500]}")
    return proc


def _git_out(*args: str, timeout: int = _GIT_TIMEOUT_S) -> str:
    return _git(*args, timeout=timeout).stdout.strip()


def restart_sentinel_path() -> Path:
    # Lives under the gitignored .tmp/ dir so its presence never dirties the
    # working tree (a dirty tree would otherwise block the next fast-forward).
    return _repo_root() / ".tmp" / "restart.request"


def write_restart_sentinel(reason: str) -> str:
    """Drop a restart-request file the ``start_all`` supervisors poll for.

    We deliberately do NOT signal the process ourselves: ``start_all.sh`` tears
    the whole stack down when the backend exits, and a console-group signal on
    Windows could hit the launcher too. Letting the supervisor own the bounce is
    the only safe, cross-platform path — it kills just the backend and relaunches
    it on the freshly pulled code.
    """
    path = restart_sentinel_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat()
    path.write_text(f"{stamp} {reason}".strip() + "\n", encoding="utf-8")
    return str(path)


def check_for_update(*, fetch: bool = True) -> dict[str, Any]:
    """Report how the local checkout compares to the tracked remote branch.

    Never mutates the checkout. ``fetch=False`` skips the network round-trip and
    compares against the last-known remote ref (used for the lightweight startup
    check so it can't hang the UI).
    """
    current_version = getattr(forven, "__version__", "")

    if not _is_git_checkout():
        return {
            "supported": False,
            "reason": "Not a git checkout — install was not cloned from the repo, so it can't self-update.",
            "current_version": current_version,
            "update_available": False,
            "can_apply": False,
        }

    remote = _target_remote()
    branch = _target_branch()
    remote_ref = f"{remote}/{branch}"

    fetch_error = ""
    if fetch:
        try:
            _git("fetch", "--quiet", remote, branch, timeout=_FETCH_TIMEOUT_S)
        except RuntimeError as exc:
            # Offline / transient remote failure: still report local state so the
            # UI can show "couldn't reach remote" rather than a hard error.
            fetch_error = str(exc)
            log.info("self-update fetch failed: %s", fetch_error)

    try:
        current_sha = _git_out("rev-parse", "HEAD")
        current_branch = _git_out("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git_out("status", "--porcelain"))

        # The remote-tracking ref may not exist (e.g. branch never fetched).
        remote_known = _git("rev-parse", "--verify", "--quiet", remote_ref, check=False).returncode == 0
        if not remote_known:
            return {
                "supported": True,
                "reason": fetch_error or f"Remote ref {remote_ref} is unknown locally.",
                "current_version": current_version,
                "current_branch": current_branch,
                "target_branch": branch,
                "target_remote": remote,
                "current_sha": current_sha,
                "current_sha_short": current_sha[:9],
                "update_available": False,
                "can_apply": False,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }

        remote_sha = _git_out("rev-parse", remote_ref)
        behind = int(_git_out("rev-list", "--count", f"HEAD..{remote_ref}") or "0")
        ahead = int(_git_out("rev-list", "--count", f"{remote_ref}..HEAD") or "0")
        latest_subject = _git_out("log", "-1", "--format=%s", remote_ref)
        latest_date = _git_out("log", "-1", "--format=%cI", remote_ref)
    except RuntimeError as exc:
        return {
            "supported": True,
            "reason": str(exc),
            "current_version": current_version,
            "target_branch": branch,
            "target_remote": remote,
            "update_available": False,
            "can_apply": False,
        }

    on_target_branch = current_branch == branch
    update_available = behind > 0

    # We only ever fast-forward. Spell out why an available update can't be
    # auto-applied so the UI can tell the operator what to fix.
    blocked_reason = ""
    if update_available:
        if not on_target_branch:
            blocked_reason = (
                f"You're on branch '{current_branch}', but the updater tracks '{branch}'. "
                f"Switch to '{branch}' to update from inside the app."
            )
        elif ahead > 0:
            blocked_reason = (
                f"Local checkout has {ahead} commit(s) not on '{remote_ref}', so it can't fast-forward. "
                "Push or reset those commits first."
            )
        elif dirty:
            blocked_reason = "Working tree has uncommitted changes. Commit or stash them, then update."

    can_apply = update_available and not blocked_reason

    return {
        "supported": True,
        "reason": fetch_error,
        "current_version": current_version,
        "current_branch": current_branch,
        "target_branch": branch,
        "target_remote": remote,
        "on_target_branch": on_target_branch,
        "current_sha": current_sha,
        "current_sha_short": current_sha[:9],
        "remote_sha": remote_sha,
        "remote_sha_short": remote_sha[:9],
        "behind": behind,
        "ahead": ahead,
        "dirty": dirty,
        "update_available": update_available,
        "can_apply": can_apply,
        "blocked_reason": blocked_reason,
        "latest_commit_subject": latest_subject,
        "latest_commit_date": latest_date,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


def apply_update() -> dict[str, Any]:
    """Fast-forward the checkout to the tracked remote branch.

    Returns a dict whose ``restart_pending`` flag tells the caller whether new
    code was pulled and the process should restart. Raises ``RuntimeError`` only
    on unexpected git failures; expected refusals (dirty tree, wrong branch,
    nothing to do) come back as a structured ``blocked``/``noop`` status.
    """
    if not _APPLY_LOCK.acquire(blocking=False):
        return {"status": "busy", "restart_pending": False, "reason": "An update is already in progress."}
    try:
        status = check_for_update(fetch=True)

        if not status.get("supported"):
            return {"status": "unsupported", "restart_pending": False, "reason": status.get("reason", "")}
        if not status.get("update_available"):
            return {
                "status": "noop",
                "restart_pending": False,
                "reason": "Already up to date.",
                "current_sha": status.get("current_sha"),
            }
        if not status.get("can_apply"):
            return {"status": "blocked", "restart_pending": False, "reason": status.get("blocked_reason", "")}

        remote = status["target_remote"]
        branch = status["target_branch"]
        from_sha = status.get("current_sha", "")

        log.info("self-update: git pull --ff-only %s %s", remote, branch)
        pull = _git("pull", "--ff-only", remote, branch, timeout=_FETCH_TIMEOUT_S)
        to_sha = _git_out("rev-parse", "HEAD")

        restart_pending = from_sha != to_sha
        if restart_pending:
            write_restart_sentinel(f"self-update {from_sha[:9]}->{to_sha[:9]}")

        log.info("self-update applied: %s -> %s", from_sha[:9], to_sha[:9])
        return {
            "status": "updated",
            "restart_pending": restart_pending,
            "from_sha": from_sha,
            "to_sha": to_sha,
            "from_sha_short": from_sha[:9],
            "to_sha_short": to_sha[:9],
            "pull_output": (pull.stdout or pull.stderr or "").strip()[:2000],
        }
    finally:
        _APPLY_LOCK.release()
