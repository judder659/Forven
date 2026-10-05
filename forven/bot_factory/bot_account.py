"""Set up, inspect and check the separate OS account bots run as.

The mechanics live in ``forven.bot_factory.os_isolation``; this module holds
the operator-side glue (CLI and setup script), kept out of the bot manager so
the manager's imports stay small.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from forven.bot_factory import os_isolation
from forven.bot_factory.broker_client import NO_MASTER_KEY_ENV
from forven.bot_factory.manager import bot_account_file, load_bot_os_account
from forven.config import FORVEN_DB, FORVEN_HOME
from forven.secret_storage import (
    _key_path,
    _legacy_key_path,
    _preferred_key_path,
    _restrict_to_owner,
    encrypt_secret,
)
from forven.security.env_allowlist import build_subprocess_env


def save_bot_os_account(username: str, password: str) -> Path:
    """Record the account bots run as, with the password encrypted."""
    username = str(username or "").strip()
    if not username or not password:
        raise ValueError("Both a username and a password are required")
    if not os_isolation._is_windows():
        raise os_isolation.BotIsolationError("Running bots as a separate account is only supported on Windows")
    path = bot_account_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"username": username, "password": encrypt_secret(password)}),
        encoding="utf-8",
    )
    _restrict_to_owner(path)
    return path


def clear_bot_os_account() -> bool:
    """Turn isolation off. True when a record was removed."""
    path = bot_account_file()
    if not path.exists():
        return False
    path.unlink()
    return True


def bot_isolation_status() -> dict:
    """A summary for the CLI and API: never includes the password."""
    try:
        account = load_bot_os_account()
    except os_isolation.BotIsolationError as exc:
        return {"enabled": True, "ok": False, "username": None, "error": str(exc)}
    if account is None:
        return {"enabled": False, "ok": True, "username": None, "error": None}
    return {"enabled": True, "ok": True, "username": account.username, "error": None}


def check_bot_isolation(timeout: float = 60.0) -> dict:
    """Start a short probe as the bot account and report what it can reach.

    Passing means the probe ran as the account, could import Forven and open the
    database (what a bot needs), and could read none of the operator's secret
    files (the master key, wherever it lives, and the account record itself).
    """
    account = load_bot_os_account()
    if account is None:
        return {"ok": False, "problems": ["Bot isolation is not set up."]}

    secrets_to_probe = sorted(
        {str(p) for p in (_key_path(), _preferred_key_path(), _legacy_key_path(), bot_account_file()) if p.exists()}
    )
    log_path = os_isolation.bot_runtime_dir(FORVEN_HOME) / "isolation-check.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    env = os_isolation.apply_account_env(build_subprocess_env(), account, FORVEN_HOME)
    env["FORVEN_HOME"] = str(FORVEN_HOME)
    env[NO_MASTER_KEY_ENV] = "1"
    process = os_isolation.spawn_as_account(
        account, "isolation-check",
        [sys.executable, "-c", os_isolation.PROBE_SCRIPT, str(FORVEN_DB), *secrets_to_probe],
        env=env, cwd=Path(__file__).resolve().parents[2], log_path=log_path,
    )
    try:
        process.wait(timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return {"ok": False, "problems": [f"The probe did not finish within {int(timeout)}s."]}
    finally:
        process.close()

    output = log_path.read_text(encoding="utf-8", errors="replace")
    return os_isolation.judge_probe(output, account)
