"""Shared dethrone deny-cooldown state.

When the operator DENIES a strategy_dethrone_recommendation approval, the
strategy enters a deny cooldown: every dethrone-approval creation path
(brain.transition_stage's operator gate, policy's challenger/repeated-failure
paths) suppresses new recommendations for that strategy until the cooldown
expires. Repeated denials escalate the window (24h -> 72h -> 168h). The
cooldown clears when a dethrone approval is APPROVED or when the strategy
actually leaves an operator-owned stage.

This is a leaf module (imports only forven.db, lazily) so brain, policy and
control_plane can all import it at module top without cycles.

Every function takes an optional ``conn``. brain.transition_stage holds the
single WAL writer connection for its whole body, so any kv read/write made
from inside it MUST go through that connection — opening a nested get_db()
write there self-deadlocks. Callers outside a held transaction can omit
``conn`` and get the ordinary kv helpers.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

log = logging.getLogger("forven.dethrone_cooldown")

_COOLDOWN_KEY_PREFIX = "forven:dethrone:cooldown:"

# Escalating deny windows: 1st deny, 2nd deny, 3rd-and-later denies.
DENY_COOLDOWN_ESCALATION_HOURS = (24, 72, 168)


def _cooldown_key(strategy_id: str) -> str:
    return f"{_COOLDOWN_KEY_PREFIX}{str(strategy_id or '').strip()}"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_utc(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _kv_read(key: str, conn=None):
    if conn is not None:
        try:
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table: kv" in str(exc).lower():
                return None
            raise
        if not row:
            return None
        raw_value = row["value"]
        if isinstance(raw_value, (str, bytes, bytearray)):
            try:
                return json.loads(raw_value)
            except Exception:
                return None
        return raw_value

    from forven.db import kv_get

    return kv_get(key)


def _kv_write(key: str, value, conn=None) -> None:
    if conn is not None:
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json.dumps(value), _now_utc().isoformat()),
        )
        return

    from forven.db import kv_set

    kv_set(key, value)


def get_dethrone_cooldown_state(strategy_id: str, conn=None) -> dict:
    """Current cooldown state: {"until", "deny_count", "last_denied_at"}.

    Legacy compatibility: values written before escalation existed were a bare
    ISO expiry string — those parse as deny_count=1 with that expiry.
    """
    empty = {"until": None, "deny_count": 0, "last_denied_at": None}
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return empty
    try:
        raw = _kv_read(_cooldown_key(strategy_id), conn=conn)
    except Exception:
        log.warning("dethrone cooldown read failed for %s", strategy_id, exc_info=True)
        return empty
    if isinstance(raw, str) and raw.strip():
        return {"until": raw.strip(), "deny_count": 1, "last_denied_at": None}
    if isinstance(raw, dict):
        try:
            deny_count = max(0, int(raw.get("deny_count") or 0))
        except Exception:
            deny_count = 0
        until = raw.get("until")
        return {
            "until": str(until).strip() if until else None,
            "deny_count": deny_count,
            "last_denied_at": raw.get("last_denied_at") or None,
        }
    return empty


def dethrone_cooldown_active_until(strategy_id: str, conn=None) -> str | None:
    """ISO expiry while the deny cooldown is active, else None.

    The stored value IS the expiry timestamp. Unparseable values fail OPEN
    (None): the safe fallback for this gate is "surface the approval", not
    "suppress recommendations forever".
    """
    state = get_dethrone_cooldown_state(strategy_id, conn=conn)
    until = _parse_iso_utc(state.get("until"))
    if until is None:
        return None
    if _now_utc() < until:
        return until.isoformat()
    return None


def record_dethrone_deny(strategy_id: str, conn=None) -> dict:
    """Bump the deny count and arm the escalated cooldown; returns new state.

    The prior deny_count is honored even when the previous window already
    expired, so a strategy the operator keeps denying escalates 24h -> 72h ->
    168h instead of restarting at 24h each time.
    """
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return {"until": None, "deny_count": 0, "last_denied_at": None}
    prior = get_dethrone_cooldown_state(strategy_id, conn=conn)
    deny_count = int(prior.get("deny_count") or 0) + 1
    hours = DENY_COOLDOWN_ESCALATION_HOURS[
        min(deny_count - 1, len(DENY_COOLDOWN_ESCALATION_HOURS) - 1)
    ]
    now = _now_utc()
    state = {
        "until": (now + timedelta(hours=hours)).isoformat(),
        "deny_count": deny_count,
        "last_denied_at": now.isoformat(),
    }
    _kv_write(_cooldown_key(strategy_id), state, conn=conn)
    return state


def clear_dethrone_cooldown(strategy_id: str, conn=None) -> None:
    """Reset the cooldown AND the deny count (approve / stage-exit path)."""
    strategy_id = str(strategy_id or "").strip()
    if not strategy_id:
        return
    _kv_write(_cooldown_key(strategy_id), None, conn=conn)
