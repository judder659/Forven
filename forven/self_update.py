"""In-app self-update via git.

Forven is run from a git checkout (users clone the repo and launch it with
``start_all``). This module lets the running app see when a newer version is
available and fast-forward the local checkout to it — the update pill, the
"What's new" panel and Settings → System → Updates all call into here.

There are two update channels:

- ``stable`` (the default): only *releases* are offered. A release is an
  annotated ``vX.Y.Z`` tag on a commit of the tracked branch, cut weekly by the
  release workflow. Its tag message carries the release notes (see
  ``parse_release_notes``), so an install gets them with a plain ``git fetch`` —
  no GitHub API calls.
- ``latest``: every commit on the tracked branch is offered as soon as it lands
  (the old behaviour; for the maintainer's own install and testers).

The channel comes from ``FORVEN_UPDATE_CHANNEL`` when set, otherwise from the
choice saved in Settings (``$FORVEN_HOME/update_channel.json``).

Applying an update is ``git fetch`` + ``git merge --ff-only <target>``, where
the target is the release's commit (stable) or the remote branch head (latest).
The process restart is handled by the ``start_all`` supervisor, which polls for
the restart sentinel this module writes.

Design choices, all on the side of "never surprise the operator":
- We track a fixed remote/branch (default ``origin``/``main``) — configurable
  via ``FORVEN_UPDATE_REMOTE`` / ``FORVEN_UPDATE_BRANCH``.
- We only ever fast-forward. A dirty working tree, local commits the target
  doesn't have, or sitting on a different branch all *block* the apply with a
  clear reason rather than rewriting the operator's state.
"""

from __future__ import annotations

import json
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
_RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

CHANNELS = ("stable", "latest")
DEFAULT_CHANNEL = "stable"

# How many releases the history list shows.
_HISTORY_LIMIT = 10
# How many pending commit subjects the latest channel lists.
_PENDING_COMMITS_LIMIT = 30
_NOTE_SECTIONS = ("New", "Fixes", "Safety")

# Serialize apply so two concurrent clicks can't race two merges.
_APPLY_LOCK = threading.Lock()

_FETCH_TIMEOUT_S = 60
_GIT_TIMEOUT_S = 30
# The update check calls several short-lived git commands in one go.  A
# console-subsystem executable launched by the windowless backend can otherwise
# create a visible console for each command on Windows, producing a burst of
# flashing command-prompt windows during page startup.
_GIT_CREATION_FLAGS = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000) if os.name == "nt" else 0
)


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
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            shell=False,
            timeout=timeout,
            creationflags=_GIT_CREATION_FLAGS,
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


def _is_ancestor(older: str, newer: str) -> bool:
    return _git("merge-base", "--is-ancestor", older, newer, check=False).returncode == 0


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


# ---------------------------------------------------------------------------
# Channel
# ---------------------------------------------------------------------------


def _channel_file() -> Path:
    from forven.config import FORVEN_HOME

    return Path(FORVEN_HOME) / "update_channel.json"


def get_channel() -> dict[str, Any]:
    """Return ``{"channel", "locked"}``; ``locked`` means the env var decides."""
    env_value = _get_env("FORVEN_UPDATE_CHANNEL").lower()
    if env_value in CHANNELS:
        return {"channel": env_value, "locked": True}
    try:
        saved = json.loads(_channel_file().read_text(encoding="utf-8"))
        value = str(saved.get("channel") or "").strip().lower()
        if value in CHANNELS:
            return {"channel": value, "locked": False}
    except (OSError, ValueError, AttributeError):
        pass
    return {"channel": DEFAULT_CHANNEL, "locked": False}


def set_channel(channel: str) -> dict[str, Any]:
    value = str(channel or "").strip().lower()
    if value not in CHANNELS:
        raise ValueError(f"Unknown update channel {channel!r}; expected one of {', '.join(CHANNELS)}.")
    path = _channel_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"channel": value}) + "\n", encoding="utf-8")
    return get_channel()


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------


def parse_release_notes(message: str) -> dict[str, Any]:
    """Parse a release tag message into a title, urgency flag and sections.

    The release workflow writes messages like::

        Forven 0.2.0
        Urgent: no

        New:
        - Strategies show what they wait on
        Fixes:
        - ...

    A section starts at a ``New:`` / ``Fixes:`` / ``Safety:`` line or a
    ``## Heading`` (git strips ``#`` lines from tag messages unless the tag is
    made with ``--cleanup=verbatim``, so the release workflow uses the colon
    form). Anything outside a section lands in an untitled one, so a
    hand-written tag message still shows up as notes.
    """
    lines = (message or "").replace("\r\n", "\n").split("\n")
    # Drop a trailing signature block from signed tags.
    for idx, line in enumerate(lines):
        if line.startswith("-----BEGIN PGP SIGNATURE-----") or line.startswith("-----BEGIN SSH SIGNATURE-----"):
            lines = lines[:idx]
            break

    title = ""
    urgent = False
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not title and not line.startswith(("#", "-", "*")):
            title = line
            continue
        lowered = line.lower()
        if lowered.startswith("urgent:"):
            urgent = lowered.split(":", 1)[1].strip() in {"yes", "true", "1"}
            continue
        header = ""
        if line.startswith("#"):
            header = line.lstrip("#").strip()
        elif line.endswith(":") and line[:-1].strip().title() in _NOTE_SECTIONS:
            header = line[:-1].strip().title()
        if header:
            current = {"title": header, "items": []}
            sections.append(current)
            continue
        if current is None:
            current = {"title": "", "items": []}
            sections.append(current)
        item = line[1:].strip() if line[:1] in {"-", "*"} else line
        if item:
            current["items"].append(item)
    return {
        "title": title,
        "urgent": urgent,
        "sections": [section for section in sections if section["items"]],
    }


def _release_tags() -> list[dict[str, Any]]:
    """All ``vX.Y.Z`` tags, newest version first, with their commit and date."""
    out = _git_out(
        "for-each-ref",
        "refs/tags",
        "--format=%(refname:short)|%(objectname)|%(*objectname)|%(creatordate:iso-strict)",
    )
    tags: list[dict[str, Any]] = []
    for row in out.splitlines():
        parts = row.split("|")
        if len(parts) != 4:
            continue
        name, obj, peeled, date = parts
        match = _RELEASE_TAG_RE.match(name)
        if not match:
            continue
        tags.append(
            {
                "tag": name,
                "version": name[1:],
                "key": tuple(int(n) for n in match.groups()),
                "sha": peeled or obj,
                "date": date,
            }
        )
    tags.sort(key=lambda t: t["key"], reverse=True)
    return tags


def _release_details(tag: dict[str, Any]) -> dict[str, Any]:
    message = _git_out("tag", "-l", "--format=%(contents)", tag["tag"])
    notes = parse_release_notes(message)
    return {
        "version": tag["version"],
        "tag": tag["tag"],
        "sha": tag["sha"],
        "sha_short": tag["sha"][:9],
        "date": tag["date"],
        "title": notes["title"] or f"Forven {tag['version']}",
        "urgent": notes["urgent"],
        "sections": notes["sections"],
    }


def _current_version(current_sha: str, tags: list[dict[str, Any]]) -> tuple[str, int]:
    """Version label for HEAD and how many commits it sits past that release."""
    for tag in tags:
        if tag["sha"] == current_sha:
            return tag["version"], 0
    for tag in tags:
        if _is_ancestor(tag["sha"], current_sha):
            past = int(_git_out("rev-list", "--count", f"{tag['sha']}..{current_sha}") or "0")
            return tag["version"], past
    return getattr(forven, "__version__", ""), 0


# ---------------------------------------------------------------------------
# Check / apply
# ---------------------------------------------------------------------------


def _unsupported(reason: str, current_version: str, channel: dict[str, Any]) -> dict[str, Any]:
    return {
        "supported": False,
        "reason": reason,
        "current_version": current_version,
        "channel": channel["channel"],
        "channel_locked": channel["locked"],
        "update_available": False,
        "can_apply": False,
        "releases": [],
    }


def check_for_update(*, fetch: bool = True) -> dict[str, Any]:
    """Report how the local checkout compares to what its channel offers.

    Never mutates the checkout. ``fetch=False`` skips the network round-trip and
    compares against the last-known remote refs and tags (used for the
    lightweight page-load check so it can't hang the UI).
    """
    channel = get_channel()
    package_version = getattr(forven, "__version__", "")

    if not _is_git_checkout():
        return _unsupported(
            "Not a git checkout — install was not cloned from the repo, so it can't self-update.",
            package_version,
            channel,
        )

    remote = _target_remote()
    branch = _target_branch()
    remote_ref = f"{remote}/{branch}"

    fetch_error = ""
    if fetch:
        try:
            _git("fetch", "--quiet", "--tags", remote, branch, timeout=_FETCH_TIMEOUT_S)
        except RuntimeError as exc:
            # Offline / transient remote failure: still report local state so the
            # UI can show "couldn't reach the update server" rather than a hard error.
            fetch_error = str(exc)
            log.info("self-update fetch failed: %s", fetch_error)

    base: dict[str, Any] = {
        "supported": True,
        "channel": channel["channel"],
        "channel_locked": channel["locked"],
        "target_branch": branch,
        "target_remote": remote,
        "update_available": False,
        "can_apply": False,
        "releases": [],
    }

    try:
        current_sha = _git_out("rev-parse", "HEAD")
        current_branch = _git_out("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(_git_out("status", "--porcelain", "--untracked-files=no"))

        # The remote-tracking ref may not exist (e.g. branch never fetched).
        remote_known = _git("rev-parse", "--verify", "--quiet", remote_ref, check=False).returncode == 0

        # Releases must live on the tracked branch; a stray tag elsewhere is ignored.
        tags = _release_tags()
        if remote_known:
            tags = [tag for tag in tags if _is_ancestor(tag["sha"], remote_ref)]
        current_version, commits_past_release = _current_version(current_sha, tags)
        releases = [_release_details(tag) for tag in tags[:_HISTORY_LIMIT]]
    except RuntimeError as exc:
        return {**base, "reason": str(exc), "current_version": package_version}

    base.update(
        {
            "current_version": current_version,
            "commits_past_release": commits_past_release,
            "current_branch": current_branch,
            "on_target_branch": current_branch == branch,
            "current_sha": current_sha,
            "current_sha_short": current_sha[:9],
            "dirty": dirty,
            "releases": releases,
            "latest_release": releases[0] if releases else None,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    if not remote_known:
        return {**base, "reason": fetch_error or f"Remote ref {remote_ref} is unknown locally."}

    try:
        if channel["channel"] == "stable":
            target = _stable_target(current_sha, releases)
        else:
            target = _latest_target(current_sha, remote_ref)
    except RuntimeError as exc:
        return {**base, "reason": str(exc)}

    update_available = target["update_available"]

    # We only ever fast-forward. Spell out why an available update can't be
    # applied so the UI can tell the operator what to fix.
    blocked_reason = ""
    if update_available:
        if current_branch != branch:
            blocked_reason = (
                f"You're on branch '{current_branch}', but the updater tracks '{branch}'. "
                f"Switch to '{branch}' to update from inside the app."
            )
        elif target["ahead"] > 0:
            blocked_reason = (
                f"This copy has {target['ahead']} local commit(s) the update doesn't have, so it can't "
                "fast-forward. Push or reset those commits first."
            )
        elif dirty:
            blocked_reason = "This copy has uncommitted changes. Commit or stash them, then update."

    reason = fetch_error
    if not reason and channel["channel"] == "stable" and not releases:
        reason = "No releases have been published yet."

    return {
        **base,
        **target,
        "reason": reason,
        "blocked_reason": blocked_reason,
        "can_apply": update_available and not blocked_reason,
        "urgent": bool(update_available and target.get("urgent")),
    }


def _stable_target(current_sha: str, releases: list[dict[str, Any]]) -> dict[str, Any]:
    """Offer the newest release when this checkout is behind it."""
    if not releases:
        return {"update_available": False, "ahead": 0, "behind": 0, "target_sha": None}
    latest = releases[0]
    target_sha = latest["sha"]
    behind = int(_git_out("rev-list", "--count", f"{current_sha}..{target_sha}") or "0")
    ahead = int(_git_out("rev-list", "--count", f"{target_sha}..{current_sha}") or "0")
    # Releases this update brings, newest first: every newer release whose
    # commit HEAD doesn't have yet. An urgent one among them makes the whole
    # update urgent.
    pending = [r for r in releases if r["sha"] != current_sha and not _is_ancestor(r["sha"], current_sha)]
    update_available = behind > 0
    return {
        "update_available": update_available,
        "behind": behind,
        "ahead": ahead,
        "target_sha": target_sha,
        "target_sha_short": target_sha[:9],
        "target_version": latest["version"] if update_available else None,
        "pending_releases": pending if update_available else [],
        "urgent": any(r["urgent"] for r in pending) if update_available else False,
    }


def _latest_target(current_sha: str, remote_ref: str) -> dict[str, Any]:
    """Offer the head of the tracked branch, listing the commits it brings."""
    target_sha = _git_out("rev-parse", remote_ref)
    behind = int(_git_out("rev-list", "--count", f"HEAD..{remote_ref}") or "0")
    ahead = int(_git_out("rev-list", "--count", f"{remote_ref}..HEAD") or "0")
    subjects = _git_out(
        "log",
        f"--max-count={_PENDING_COMMITS_LIMIT}",
        "--first-parent",
        "--format=%s",
        f"HEAD..{remote_ref}",
    )
    return {
        "update_available": behind > 0,
        "behind": behind,
        "ahead": ahead,
        "target_sha": target_sha,
        "target_sha_short": target_sha[:9],
        "target_version": None,
        "pending_commits": [line for line in subjects.splitlines() if line.strip()],
        "latest_commit_subject": _git_out("log", "-1", "--format=%s", remote_ref),
        "latest_commit_date": _git_out("log", "-1", "--format=%cI", remote_ref),
    }


def apply_update(*, fetch: bool = True) -> dict[str, Any]:
    """Fast-forward the checkout to what its channel offers.

    Returns a dict whose ``restart_pending`` flag tells the caller whether new
    code was pulled and the process should restart. Raises ``RuntimeError`` only
    on unexpected git failures; expected refusals (dirty tree, wrong branch,
    nothing to do) come back as a structured ``blocked``/``noop`` status.
    ``fetch=False`` applies what the last check downloaded (the UI's Install
    step runs right after its Download step).
    """
    if not _APPLY_LOCK.acquire(blocking=False):
        return {"status": "busy", "restart_pending": False, "reason": "An update is already in progress."}
    try:
        status = check_for_update(fetch=fetch)

        if not status.get("supported"):
            return {"status": "unsupported", "restart_pending": False, "reason": status.get("reason", "")}
        if not status.get("update_available"):
            return {
                "status": "noop",
                "restart_pending": False,
                "reason": status.get("reason") or "Already up to date.",
                "current_sha": status.get("current_sha"),
            }
        if not status.get("can_apply"):
            return {"status": "blocked", "restart_pending": False, "reason": status.get("blocked_reason", "")}

        from_sha = status.get("current_sha", "")
        target_sha = status["target_sha"]

        log.info("self-update (%s): git merge --ff-only %s", status["channel"], target_sha[:9])
        merge = _git("merge", "--ff-only", target_sha, timeout=_FETCH_TIMEOUT_S)
        to_sha = _git_out("rev-parse", "HEAD")

        restart_pending = from_sha != to_sha
        if restart_pending:
            write_restart_sentinel(f"self-update {from_sha[:9]}->{to_sha[:9]}")

        log.info("self-update applied: %s -> %s", from_sha[:9], to_sha[:9])
        return {
            "status": "updated",
            "restart_pending": restart_pending,
            "channel": status["channel"],
            "from_version": status.get("current_version"),
            "to_version": status.get("target_version"),
            "from_sha": from_sha,
            "to_sha": to_sha,
            "from_sha_short": from_sha[:9],
            "to_sha_short": to_sha[:9],
            "pull_output": (merge.stdout or merge.stderr or "").strip()[:2000],
        }
    finally:
        _APPLY_LOCK.release()
