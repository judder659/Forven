"""Bot Factory alerts: trades, pauses and errors as operator notifications.

Runs in the backend's bot monitor, not in the bots: bots run sandboxed with no
Discord token, so the backend watches what they write (status changes and
trades) and turns it into Notifications entries and, where the operator's
switches allow, Discord messages.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# One monitor cycle never floods the Notifications panel with fills.
MAX_TRADE_ALERTS_PER_POLL = 10
_ALERT_STATUSES = ("paused", "error")


def _scope(bot: dict) -> str:
    return "live" if str(bot.get("execution_mode") or "paper").lower() == "live" else "paper"


def _usd(price: float) -> str:
    return f"${price:,.2f}" if abs(price) >= 1 else f"${price:.6g}"


class BotAlertWatcher:
    """Turns bot status changes and new trades into notifications.

    The first poll only records where things stand, so a backend restart does
    not re-announce bots that were already paused or trades already made.
    The data and notification functions are passed in (the bot monitor wires
    forven.db and forven.notifications), which keeps this module out of the
    import graph."""

    def __init__(
        self,
        *,
        list_bots: Callable[[], list[dict]],
        trade_events: Callable[[str, str], list[dict]],
        emit: Callable[..., dict],
    ) -> None:
        self._list_bots = list_bots
        self._trade_events = trade_events
        self._emit = emit
        self._statuses: dict[str, tuple[str, str]] | None = None
        self._trade_cursor: str | None = None

    def poll(self, now: datetime | None = None) -> list[dict]:
        now = now or datetime.now(timezone.utc)
        bots = {b["id"]: b for b in self._list_bots()}
        events: list[dict] = []
        events += self._status_alerts(bots)
        events += self._trade_alerts(bots, now)
        return events

    def _status_alerts(self, bots: dict[str, dict]) -> list[dict]:
        current = {
            bot_id: (str(b.get("runtime_status") or b.get("status") or "stopped"), str(b.get("error_message") or ""))
            for bot_id, b in bots.items()
        }
        previous, self._statuses = self._statuses, current
        if previous is None:
            return []
        events = []
        for bot_id, (status, message) in current.items():
            if status not in _ALERT_STATUSES or previous.get(bot_id) == (status, message):
                continue
            bot = bots[bot_id]
            name = bot.get("name") or bot_id
            scope = _scope(bot)
            title = f"Bot {name} {'paused' if status == 'paused' else 'stopped on an error'}"
            events.append(self._emit(
                "bot_alert",
                severity="fail" if status == "error" or scope == "live" else "warn",
                source="bot_factory",
                title=title,
                summary=message or None,
                metadata={
                    "bot_id": bot_id,
                    "bot_status": status,
                    "execution_type": scope,
                    "href": f"/bot-factory/{bot_id}",
                },
                dedupe_key=f"bot_alert:{bot_id}:{status}:{message[:80]}",
            ))
        return events

    def _trade_alerts(self, bots: dict[str, dict], now: datetime) -> list[dict]:
        # Second-resolution bounds, a little behind now: any stored time inside
        # second S (whatever its suffix) sorts after "S" and before "S+1", so
        # each trade event lands in exactly one poll window.
        bound = (now - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%S")
        cursor, self._trade_cursor = self._trade_cursor, bound
        if cursor is None:
            return []
        events = []
        for trade in self._trade_events(cursor, self._trade_cursor)[:MAX_TRADE_ALERTS_PER_POLL]:
            bot = bots.get(trade["bot_id"]) or {}
            name = bot.get("name") or trade["bot_id"]
            side = str(trade.get("direction") or "long").upper()
            pair = trade.get("pair") or "?"
            if trade["event"] == "closed":
                pnl = trade.get("pnl_usd")
                pnl_text = f" {'+' if (pnl or 0) >= 0 else '-'}${abs(pnl or 0):,.2f}" if pnl is not None else ""
                title = f"Bot {name} closed {side} {pair}{pnl_text}"
            else:
                price = trade.get("entry_price")
                title = f"Bot {name} opened {side} {pair}" + (f" at {_usd(price)}" if price else "")
            events.append(self._emit(
                "bot_trade",
                source="bot_factory",
                title=title,
                metadata={
                    "bot_id": trade["bot_id"],
                    "trade_id": trade["id"],
                    "trade_event": trade["event"],
                    "execution_type": _scope(bot),
                    "href": f"/bot-factory/{trade['bot_id']}",
                },
                dedupe_key=f"bot_trade:{trade['id']}:{trade['event']}",
            ))
        return events
