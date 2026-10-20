"""The live gate for bots: what a bot must show before GO LIVE unlocks.

It mirrors the strategies' live gate: a forward record of at least
``MIN_TRADES`` closed trades over at least ``MIN_DAYS`` days, in profit, plus
a replay of its current settings on older data that also made money. AI cost
is left out: most installs reach models through a subscription login. Typing GO LIVE is still required after it
passes. Pure functions; callers fetch the ledger, cost and replays.
"""

from __future__ import annotations

from datetime import datetime

from forven.bot_factory.performance import parse_time

MIN_TRADES = 20
MIN_DAYS = 30


def _check(key: str, ok: bool, label: str, detail: str) -> dict:
    return {"key": key, "ok": bool(ok), "label": label, "detail": detail}


def evaluate(
    *,
    ledger: list[dict],
    replays: list[dict],
    settings_changed_at: object,
    now: datetime,
) -> dict:
    """Each check with a plain reason, and whether all of them pass.

    ``ledger`` is the bot's trades (status, pnl_usd, opened_at); ``replays``
    are its replays, newest first, with ``status``, ``created_at`` and
    ``summary``; ``settings_changed_at`` is when its settings last changed.
    """
    closed = [t for t in ledger if t.get("status") == "CLOSED" and t.get("pnl_usd") is not None]
    first = min((parse_time(t.get("opened_at")) for t in ledger if parse_time(t.get("opened_at"))), default=None)
    days = (now - first).total_seconds() / 86400 if first else 0.0
    pnl = sum(float(t["pnl_usd"]) for t in closed)

    changed = parse_time(settings_changed_at)
    current = [
        r for r in replays
        if r.get("status") == "done" and r.get("summary")
        and (changed is None or (parse_time(r.get("created_at")) or now) >= changed)
    ]
    replay = current[0] if current else None
    replay_pnl = (replay or {}).get("summary", {}).get("pnl_usd")

    checks = [
        _check(
            "trades", len(closed) >= MIN_TRADES, f"{MIN_TRADES}+ closed trades",
            f"{len(closed)} closed so far.",
        ),
        _check(
            "days", days >= MIN_DAYS, f"{MIN_DAYS}+ days of trading",
            f"{days:.0f} days since its first trade." if first else "No trades yet.",
        ),
        _check(
            "profit", bool(closed) and pnl > 0, "In profit",
            f"${pnl:,.2f} from closed trades.",
        ),
        _check(
            "replay", replay is not None and replay_pnl is not None and float(replay_pnl) > 0,
            "A profitable replay of its current settings",
            (
                f"Latest replay ({str(replay.get('window_start'))[:10]} to {str(replay.get('window_end'))[:10]}) "
                f"made ${float(replay_pnl or 0):,.2f}."
                if replay else
                "No finished replay since its settings last changed. Run one from the Replay tab."
            ),
        ),
    ]
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def refusal(result: dict) -> str | None:
    """The message GO LIVE shows when the gate hasn't passed, else None."""
    if result.get("passed"):
        return None
    missing = [c for c in result.get("checks", []) if not c["ok"]]
    return "This bot hasn't passed the live gate yet: " + " ".join(
        f"{c['label']}: {c['detail']}" for c in missing
    )
