import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(tags=["webhooks"])
log = logging.getLogger("forven.webhooks")
_PULL_LOCK = threading.Lock()
_REPLAY_LOCK = threading.Lock()
_RECENT_DELIVERIES: dict[str, float] = {}

# Defense in depth — even though subprocess uses argv (no shell), reject
# operator-supplied env values containing characters that would be dangerous
# if the call site ever changed to shell=True.
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_GIT_REMOTE_RE = re.compile(r"^[A-Za-z0-9._\-]+$")


def _validate_git_ref(value: str, *, field: str) -> str:
    if not value or not _GIT_REF_RE.match(value):
        raise RuntimeError(f"Invalid {field}: {value!r}")
    return value


def _validate_git_remote(value: str, *, field: str) -> str:
    if not value or not _GIT_REMOTE_RE.match(value):
        raise RuntimeError(f"Invalid {field}: {value!r}")
    return value


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _get_env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _webhook_secret() -> str:
    return _get_env("FORVEN_GITHUB_WEBHOOK_SECRET")


def _target_branch() -> str:
    return _get_env("FORVEN_GITHUB_WEBHOOK_BRANCH", "main")


def _target_remote() -> str:
    return _get_env("FORVEN_GITHUB_WEBHOOK_REMOTE", "origin")


def _target_repo_path() -> str:
    default_path = str(_repo_root())
    return _get_env("FORVEN_GITHUB_WEBHOOK_REPO", default_path)


def _post_pull_command() -> str:
    return _get_env("FORVEN_GITHUB_WEBHOOK_POST_PULL_CMD")


def _delivery_cache_window_seconds() -> int:
    raw = _get_env("FORVEN_GITHUB_WEBHOOK_MAX_AGE_SECONDS", "300")
    try:
        return max(60, int(raw))
    except Exception:
        return 300


def _short_output(raw: str, limit: int = 2000) -> str:
    text = (raw or "").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _verify_signature(secret: str, body: bytes, signature_header: str) -> bool:
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _parse_json_body(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body.decode("utf-8") or "{}")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="Webhook payload must be a JSON object")
    return parsed


def _parse_iso8601_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_event_timestamp(payload: dict[str, Any]) -> datetime | None:
    head_commit = payload.get("head_commit")
    if isinstance(head_commit, dict):
        parsed = _parse_iso8601_timestamp(head_commit.get("timestamp"))
        if parsed is not None:
            return parsed

    repository = payload.get("repository")
    if isinstance(repository, dict):
        pushed_at = repository.get("pushed_at")
        if isinstance(pushed_at, (int, float)) and pushed_at > 0:
            return datetime.fromtimestamp(float(pushed_at), tz=timezone.utc)
        parsed = _parse_iso8601_timestamp(repository.get("updated_at"))
        if parsed is not None:
            return parsed

    return None


def _reset_webhook_replay_cache() -> None:
    """Clear in-memory and persistent dedup state. Used by tests."""
    with _REPLAY_LOCK:
        _RECENT_DELIVERIES.clear()
    try:
        from forven.db import get_db
        with get_db() as conn:
            conn.execute("DELETE FROM webhook_deliveries WHERE source = 'github'")
    except Exception:
        pass


def _claim_delivery_id(delivery_id: str, payload: dict[str, Any]) -> None:
    normalized_delivery_id = str(delivery_id or "").strip()
    if not normalized_delivery_id:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Delivery header")

    event_timestamp = _extract_event_timestamp(payload)
    if event_timestamp is None:
        raise HTTPException(status_code=400, detail="Webhook payload is missing a replay-check timestamp")

    now_utc = datetime.now(timezone.utc)
    max_age_seconds = _delivery_cache_window_seconds()
    event_age_seconds = abs((now_utc - event_timestamp).total_seconds())
    if event_age_seconds > max_age_seconds:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Webhook event is outside the replay window: "
                f"{event_age_seconds:.0f}s old (limit {max_age_seconds}s)"
            ),
        )

    expiry_ts = now_utc.timestamp() + max_age_seconds
    now_ts = now_utc.timestamp()

    # H-S2: persistent dedup via webhook_deliveries table — survives
    # process restart so a delivery replayed after the in-memory cache is
    # rebuilt is still rejected. INSERT OR FAIL provides atomic claim.
    try:
        from forven.db import get_db
        expires_iso = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).isoformat()
        with get_db() as conn:
            # Sweep expired rows so the table doesn't grow forever.
            conn.execute(
                "DELETE FROM webhook_deliveries WHERE expires_at <= ? AND source = 'github'",
                (datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),),
            )
            try:
                conn.execute(
                    "INSERT INTO webhook_deliveries (delivery_id, source, expires_at) "
                    "VALUES (?, 'github', ?)",
                    (normalized_delivery_id, expires_iso),
                )
            except Exception as exc:
                # IntegrityError on duplicate primary key is the expected
                # rejection path. Any other DB error: log and fall back to
                # the in-memory cache so we don't block on a transient DB
                # hiccup. (Most likely a race; the persistent layer will
                # catch the next replay anyway.)
                from sqlite3 import IntegrityError
                if isinstance(exc, IntegrityError):
                    raise HTTPException(
                        status_code=409,
                        detail=f"Duplicate webhook delivery: {normalized_delivery_id}",
                    ) from exc
                log.warning("Persistent webhook dedup write failed (%s); falling through to memory", exc)
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("Persistent webhook dedup unavailable (%s); using in-memory only", exc)

    with _REPLAY_LOCK:
        expired_ids = [key for key, expires_at in _RECENT_DELIVERIES.items() if expires_at <= now_ts]
        for expired_id in expired_ids:
            _RECENT_DELIVERIES.pop(expired_id, None)
        if normalized_delivery_id in _RECENT_DELIVERIES:
            raise HTTPException(status_code=409, detail=f"Duplicate webhook delivery: {normalized_delivery_id}")
        _RECENT_DELIVERIES[normalized_delivery_id] = expiry_ts


def _git_pull(repo_path: str, remote: str, branch: str) -> dict[str, Any]:
    safe_remote = _validate_git_remote(remote, field="FORVEN_GITHUB_WEBHOOK_REMOTE")
    safe_branch = _validate_git_ref(branch, field="FORVEN_GITHUB_WEBHOOK_BRANCH")

    log.info("webhook git fetch: remote=%s repo=%s", safe_remote, repo_path)
    fetch = subprocess.run(
        ["git", "-C", repo_path, "fetch", safe_remote],
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    if fetch.returncode != 0:
        raise RuntimeError(
            f"git fetch failed ({fetch.returncode}): {_short_output(fetch.stderr or fetch.stdout)}"
        )

    log.info("webhook git pull --ff-only: remote=%s branch=%s", safe_remote, safe_branch)
    pull = subprocess.run(
        ["git", "-C", repo_path, "pull", "--ff-only", safe_remote, safe_branch],
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    if pull.returncode != 0:
        raise RuntimeError(
            f"git pull failed ({pull.returncode}): {_short_output(pull.stderr or pull.stdout)}"
        )

    return {
        "fetch_output": _short_output(fetch.stdout or fetch.stderr),
        "pull_output": _short_output(pull.stdout or pull.stderr),
    }


def _run_post_pull(command: str, repo_path: str) -> str:
    if not command:
        return ""
    try:
        argv = shlex.split(command)
    except Exception as exc:
        raise RuntimeError(f"Invalid FORVEN_GITHUB_WEBHOOK_POST_PULL_CMD: {exc}") from exc

    if not argv:
        return ""

    log.info("webhook post-pull exec: argv0=%s argc=%d cwd=%s", argv[0], len(argv), repo_path)
    completed = subprocess.run(
        argv,
        cwd=repo_path,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"post-pull command failed ({completed.returncode}): "
            f"{_short_output(completed.stderr or completed.stdout)}"
        )
    return _short_output(completed.stdout or completed.stderr)


@router.get("/api/webhooks/github/health")
def github_webhook_health():
    # SECURITY (audit 2026-06-22, L11): this endpoint is auth-exempt (the webhook
    # prefix is unauthenticated by design). Do NOT disclose the absolute repo
    # path / remote / branch here — that is filesystem reconnaissance for an
    # unauthenticated caller. Return only booleans.
    return {
        "status": "ok",
        "branch_configured": bool(_target_branch()),
        "remote_configured": bool(_target_remote()),
        "post_pull_configured": bool(_post_pull_command()),
        "secret_configured": bool(_webhook_secret()),
    }


@router.post("/api/webhooks/github")
async def github_webhook(request: Request):
    secret = _webhook_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail="Webhook disabled: FORVEN_GITHUB_WEBHOOK_SECRET is not configured",
        )

    body = await request.body()
    signature = str(request.headers.get("x-hub-signature-256", "")).strip()
    if not signature:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256 header")
    if not _verify_signature(secret, body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    event = str(request.headers.get("x-github-event", "")).strip().lower()
    if event == "ping":
        payload = _parse_json_body(body)
        return {
            "status": "ok",
            "message": "ping acknowledged",
            "hook_id": payload.get("hook_id"),
        }

    if event != "push":
        return {"status": "ignored", "reason": f"Unsupported event '{event}'"}

    payload = _parse_json_body(body)
    delivery_id = str(request.headers.get("x-github-delivery", "")).strip()
    pushed_ref = str(payload.get("ref") or "")
    target_ref = f"refs/heads/{_target_branch()}"
    if pushed_ref != target_ref:
        return {
            "status": "ignored",
            "reason": f"Push ref '{pushed_ref}' does not match '{target_ref}'",
        }
    _claim_delivery_id(delivery_id, payload)

    acquired = _PULL_LOCK.acquire(blocking=False)
    if not acquired:
        return {"status": "busy", "reason": "Update already in progress"}

    repo_path = _target_repo_path()
    remote = _target_remote()
    branch = _target_branch()
    post_pull = _post_pull_command()

    try:
        pull_result = _git_pull(repo_path=repo_path, remote=remote, branch=branch)
        post_result = _run_post_pull(post_pull, repo_path) if post_pull else ""
        log.info(
            "GitHub webhook applied update for %s/%s at %s",
            remote,
            branch,
            repo_path,
        )
        return {
            "status": "updated",
            "repo_path": repo_path,
            "remote": remote,
            "branch": branch,
            "fetch_output": pull_result.get("fetch_output", ""),
            "pull_output": pull_result.get("pull_output", ""),
            "post_pull_output": post_result,
            "after": payload.get("after"),
        }
    except Exception as exc:
        log.exception("GitHub webhook update failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        _PULL_LOCK.release()
