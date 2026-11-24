"""Live netting engine: many live strategies share each coin on one account.

Hyperliquid keeps ONE net position per coin per account. Without this engine
Forven therefore lets a single live strategy hold a coin at a time and refuses
every other entry. With it:

* each live trade stays its own row (size, direction, entry, stop, target);
* the exchange holds only the **sum** of the rows on a coin. An entry or exit
  sends one order for that strategy's size and the exchange nets it, so a long
  and a short on the same coin offset instead of refusing each other;
* **Forven runs every strategy's stop and target** (``forven.mark_watcher``),
  because one exchange position cannot carry many independent stops;
* one **disaster stop** per coin rests on the exchange for the whole net
  position, just beyond the furthest strategy stop on the net side. It only
  fires when Forven was not there to close the rows first;
* live capital is divided into **slots**, a slot being one full-size position.
  The slot count comes from how many of the live strategies have been in a
  trade at once, capped by what the install's own balance can carry;
* the books are checked against the exchange by **net position per coin**. A
  difference that persists blocks new entries on that coin and is shown to the
  operator, who decides how to resolve it. The engine never trades on its own
  to "fix" a difference.

Master wallet only: it will not turn on while direction books (sub-accounts)
are enabled. Turning it on takes over open live positions; turning it off waits
for them to close.

On by default: an install that has never chosen is switched over by the daemon
(``auto_activate``). An operator can turn it off on the Risk page.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from forven.db import get_db, kv_get, kv_set

log = logging.getLogger("forven.live_netting")

# Its own key, not the shared settings blob: that blob is rewritten whole by the
# Settings page, and the switch must only ever change through set_enabled(),
# which refuses while a live position is open.
SETTINGS_KV_KEY = "forven:live_netting:config"
SLOTS_KV_KEY = "forven:live_netting:slots"
SWEEP_KV_KEY = "forven:live_netting:last_sweep"

DEFAULTS = {
    "live_netting_enabled": False,
    # Share of time the slots must cover; the rest is when an entry is blocked.
    "live_netting_slot_coverage_pct": 95.0,
    # The disaster stop sits this far beyond the furthest strategy stop.
    "live_netting_disaster_gap_pct": 0.5,
    # Operator's own slot count. 0 = calculated.
    "live_netting_slots_override": 0,
}

MIN_ORDER_USD = 10.0
HISTORY_DAYS = 180
SLOT_REFRESH_SECONDS = 3600.0
# A row with no stop of its own still needs a level for the disaster stop.
NO_STOP_FALLBACK_FRAC = 0.10
# A difference between the books and the exchange must be seen on this many
# sweeps in a row before it counts: an order in flight looks like one briefly.
MISMATCH_CONFIRM_SWEEPS = 2
ORDER_SETTLE_SECONDS = 5.0
PENDING_CLOSE_GRACE_SECONDS = 120.0
# After a trade's close order fills, a second close for the same trade is
# refused for this long: by then the first caller has closed the row.
CLOSE_CLAIM_SECONDS = 20.0

# Close refusals that are the engine waiting, not an execution failure: the
# caller retries and must not alert or hand the strategy back for review.
_HOLD_MARKERS = ("not sending another close", "close held for retry")


def is_hold(reason: object) -> bool:
    text = str(reason or "")
    return any(marker in text for marker in _HOLD_MARKERS)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS live_netting_stops (
    coin       TEXT PRIMARY KEY,
    oid        TEXT,
    direction  TEXT NOT NULL,
    size       REAL NOT NULL,
    price      REAL NOT NULL,
    placed_at  TEXT,
    error      TEXT
);
CREATE TABLE IF NOT EXISTS live_netting_closing (
    trade_id   TEXT PRIMARY KEY,
    at         REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS live_netting_mismatch (
    coin            TEXT PRIMARY KEY,
    book_units      REAL NOT NULL,
    exchange_units  REAL NOT NULL,
    seen            INTEGER NOT NULL DEFAULT 1,
    first_seen      TEXT,
    last_seen       TEXT,
    note            TEXT
);
"""

_enabled_cache: dict = {"at": 0.0, "value": False}
_ENABLED_TTL_SECONDS = 2.0
_coin_locks: dict[str, threading.RLock] = {}
_coin_locks_guard = threading.Lock()
_last_order_at: dict[str, float] = {}
# trade_id -> (coin, signed units, time): a close that has filled on the exchange
# while its row is still OPEN for the instant before the caller books it.
_filled_closes: dict[str, tuple[str, float, float]] = {}
_schema_ready = [False]


# --- small helpers ---------------------------------------------------------------


def coin_of(asset: object) -> str:
    return str(asset or "").strip().upper().split("/", 1)[0]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat()


def _as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def _lock(coin: str) -> threading.RLock:
    with _coin_locks_guard:
        return _coin_locks.setdefault(coin, threading.RLock())


def _same(a: float, b: float) -> bool:
    """Two position sizes that differ only by float or lot dust."""
    return abs(a - b) <= max(1e-9, 1e-6 * max(abs(a), abs(b)))


def _ensure_schema() -> None:
    if _schema_ready[0]:
        return
    with get_db() as conn:
        conn.executescript(SCHEMA_SQL)
    _schema_ready[0] = True


def _testnet() -> bool:
    from forven.exchange.hyperliquid import resolve_configured_testnet

    return resolve_configured_testnet(default_testnet=True)


def _notify(event: str, coin: str, title: str, summary: str, *, severity: str = "critical") -> None:
    try:
        from forven.notifications import emit_notification

        emit_notification(
            event, severity=severity, source="live_netting", title=title, summary=summary,
            dedupe_key=f"{event}:{coin}",
        )
    except Exception as exc:  # noqa: BLE001 - an alert must never break an order path
        log.error("live netting: could not send %s alert for %s: %s", event, coin, exc)
    try:
        from forven.db import log_activity

        log_activity(severity if severity != "info" else "info", "live_netting", f"{title}: {summary}")
    except Exception:  # noqa: BLE001
        pass


# --- settings --------------------------------------------------------------------


def settings() -> dict:
    try:
        raw = kv_get(SETTINGS_KV_KEY, {})
    except Exception:  # noqa: BLE001
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["live_netting_enabled"] = _as_bool(raw.get("live_netting_enabled"), False)
    for key in ("live_netting_slot_coverage_pct", "live_netting_disaster_gap_pct"):
        value = _float(raw.get(key))
        if value is not None and value >= 0:
            out[key] = value
    out["live_netting_slot_coverage_pct"] = min(max(out["live_netting_slot_coverage_pct"], 50.0), 100.0)
    out["live_netting_disaster_gap_pct"] = min(out["live_netting_disaster_gap_pct"], 20.0)
    try:
        out["live_netting_slots_override"] = max(int(raw.get("live_netting_slots_override") or 0), 0)
    except (TypeError, ValueError):
        out["live_netting_slots_override"] = 0
    return out


def _books_on() -> bool:
    try:
        from forven.exchange import books

        return bool(books.books_enabled())
    except Exception:  # noqa: BLE001 - unknown routing: do not net
        return True


def enabled() -> bool:
    """True when live orders on the master wallet go through the engine.

    A failed read keeps the last known answer. Flipping to "off" on a locked
    database would hand netted trades to the one-position-per-coin rules for a
    moment, and those would misread a shared position.
    """
    now = time.time()
    if now - float(_enabled_cache["at"]) <= _ENABLED_TTL_SECONDS:
        return bool(_enabled_cache["value"])
    try:
        from forven.sim.clock import is_sim_active

        raw = kv_get(SETTINGS_KV_KEY, {})
        switch = _as_bool(raw.get("live_netting_enabled"), False) if isinstance(raw, dict) else False
        value = switch and not is_sim_active() and not _books_on()
    except Exception as exc:  # noqa: BLE001
        log.warning("live netting: switch unreadable (%s); keeping the last known state", exc)
        return bool(_enabled_cache["value"])
    _enabled_cache.update(at=now, value=value)
    return value


def active_for(vault_address: object = None) -> bool:
    """Whether an order on this account is the engine's. Sub-accounts never are."""
    return not vault_address and enabled()


def _invalidate_enabled() -> None:
    _enabled_cache.update(at=0.0, value=False)


def _switch_chosen() -> bool:
    """Whether this install has a stored choice (on or off) rather than the default."""
    raw = kv_get(SETTINGS_KV_KEY, {})
    return isinstance(raw, dict) and "live_netting_enabled" in raw


def _write_switch(value: bool | None) -> None:
    """Store the switch. None forgets the choice, so the install is back on the default."""
    raw = kv_get(SETTINGS_KV_KEY, {})
    raw = dict(raw) if isinstance(raw, dict) else {}
    if value is None:
        raw.pop("live_netting_enabled", None)
    else:
        raw["live_netting_enabled"] = bool(value)
    kv_set(SETTINGS_KV_KEY, raw)
    _invalidate_enabled()


def _mark_rows(rows: list[dict], netted: bool) -> None:
    from forven.scanner import _update_trade_signal_data

    for row in rows:
        _update_trade_signal_data(row["trade_id"], {
            "netting": netted,
            "netting_taken_over_at": _iso() if netted else None,
        })
    try:
        from forven.mark_watcher import invalidate_armed_cache

        invalidate_armed_cache()
    except Exception:  # noqa: BLE001
        pass


def _own_order_ids(row: dict) -> list[str]:
    sd = row["signal_data"]
    found = [sd.get("exchange_stop_order_id"), sd.get("exchange_take_profit_order_id"),
             *(sd.get("recovery_open_order_ids") or [])]
    return [str(oid) for oid in found if oid]


def _take_over_open_positions(rows: list[dict]) -> dict:
    """Turn the engine on with live positions open: bring them under it.

    Order matters, so a position is never without protection:
      1. the books must match the exchange on every coin, or nothing changes;
      2. the rows are marked netted and the switch goes on, so Forven's mark
         watcher starts running their stops and targets;
      3. each coin's disaster stop is placed. If any is refused, everything is
         put back as it was and each trade keeps its own exchange stop;
      4. only then are the trades' own stop and target orders cancelled.
    """
    from forven.scanner import _update_trade_signal_data

    testnet = _testnet()
    positions = exchange_positions(testnet)
    coins = sorted({r["coin"] for r in rows})
    for coin in coins:
        books, held = book_net(coin, rows), float(positions.get(coin, 0.0))
        if not _same(books, held):
            raise ValueError(
                f"{coin}: Forven's live trades add up to {books:g} but the exchange holds {held:g}. "
                "Nothing was changed. Fix that difference first, then turn the engine on."
            )

    _ensure_schema()
    chosen_before = _switch_chosen()
    _mark_rows(rows, True)
    _write_switch(True)
    failed: list[str] = []
    for coin in coins:
        try:
            if refresh_disaster_stop(coin, testnet=testnet) == "failed":
                failed.append(coin)
        except Exception as exc:  # noqa: BLE001
            log.error("live netting: takeover of %s failed: %s", coin, exc)
            failed.append(coin)
    if failed:
        for coin, stop in _tracked_stops().items():
            if stop.get("oid"):
                _cancel_stop(coin, stop["oid"], testnet)
        with get_db() as conn:
            conn.execute("DELETE FROM live_netting_stops")
        # Back to exactly what was stored before: an install that had never
        # chosen is still on the default and is tried again later.
        _write_switch(False if chosen_before else None)
        _mark_rows(rows, False)
        raise ValueError(
            f"The exchange refused the disaster stop on {', '.join(failed)}, so the engine was left off "
            "and every trade keeps its own stop. Nothing was changed."
        )

    leftover: list[str] = []
    for row in rows:
        for oid in _own_order_ids(row):
            try:
                from forven.exchange.hyperliquid import cancel_order

                result = cancel_order(row["coin"], int(oid), testnet=testnet, protective_cleanup=True)
                if isinstance(result, dict) and result.get("error"):
                    leftover.append(f"{row['coin']} #{oid}")
            except Exception:  # noqa: BLE001 - already filled or gone
                leftover.append(f"{row['coin']} #{oid}")
        _update_trade_signal_data(row["trade_id"], {
            "exchange_stop_requested": False, "exchange_take_profit_requested": False,
            "exchange_stop_order_id": None, "exchange_take_profit_order_id": None,
            "recovery_open_order_ids": [],
        })
    if leftover:
        _notify(
            "live_netting_takeover_leftover", "takeover",
            "Old stop orders may still be resting",
            "The engine is on, but these orders could not be confirmed cancelled: "
            f"{', '.join(leftover)}. Check the open orders on the exchange and cancel them by hand.",
            severity="warning",
        )
    return {"taken_over": [r["trade_id"] for r in rows], "coins": coins, "uncancelled": leftover}


AUTO_RETRY_SECONDS = 600.0
_auto_last_try = [0.0]


def auto_activate() -> str | None:
    """Switch an install over by itself. Returns what happened, None when nothing to do.

    The engine is the default way to trade live. The stored switch stays the
    one source of truth (so a position is never under two sets of rules); an
    install that has never chosen is moved onto the engine here, with any open
    live positions taken over the safe way. An operator who turned it off is
    left alone. If the takeover cannot be done cleanly it changes nothing,
    tells the operator once, and tries again later.
    """
    if _switch_chosen():
        return None  # already on, or the operator's own choice
    if _books_on():
        return None  # sub-account trading keeps the one-position-per-coin rules
    from forven.sim.clock import is_sim_active

    if is_sim_active():
        return None  # a simulation never touches the real switch or the exchange
    now = time.time()
    if now - _auto_last_try[0] < AUTO_RETRY_SECONDS:
        return None
    _auto_last_try[0] = now
    try:
        out = set_enabled(True)
    except Exception as exc:  # noqa: BLE001 - nothing was changed; retry later
        log.warning("live netting: could not switch this install over yet: %s", exc)
        _notify(
            "live_netting_auto_activation_waiting", "install",
            "Shared coins not switched on yet",
            f"Forven tried to move live trading onto the shared-coin engine and left everything as it was: {exc} "
            "It will try again in 10 minutes. You can also turn it on from the Risk page.",
            severity="warning",
        )
        return f"waiting: {exc}"
    taken = len((out.get("takeover") or {}).get("taken_over") or [])
    _notify(
        "live_netting_auto_activated", "install",
        "Live strategies can now share coins",
        "Live trading now runs on the shared-coin engine: any number of live strategies can trade the same coin, "
        "Forven runs each strategy's stop and target, and one disaster stop per coin rests on the exchange. "
        + (f"{taken} open live position(s) were taken over. " if taken else "")
        + "You can turn it off on the Risk page.",
        severity="info",
    )
    return "activated"


def set_enabled(value: bool) -> dict:
    """Turn the engine on or off.

    On: open live positions are taken over (see ``_take_over_open_positions``).
    Off: refused while a live position is open. Several strategies may share a
    coin by then, and the one-position-per-coin rules cannot hold that.
    """
    value = bool(value)
    current = bool(settings()["live_netting_enabled"])
    if value == current:
        return status()
    if value and _books_on():
        raise ValueError(
            "\"Separate long and short accounts\" (direction books) is on. The shared-coin engine runs "
            "on the main wallet only: move the USDC from both sub-accounts to the main wallet, then "
            "turn that setting off in Settings > Hyperliquid."
        )
    rows = open_rows()
    takeover = None
    if value and rows:
        takeover = _take_over_open_positions(rows)
    elif rows:
        coins = ", ".join(sorted({r["coin"] for r in rows}))
        raise ValueError(
            f"{len(rows)} live position(s) are open ({coins}). The engine can be turned off once they "
            "have closed: the old rules allow only one position per coin."
        )
    else:
        _write_switch(value)
    try:
        from forven.db import log_activity

        log_activity("warning", "live_netting", f"Live netting engine turned {'ON' if value else 'OFF'}")
    except Exception:  # noqa: BLE001
        pass
    out = status()
    if takeover:
        out["takeover"] = takeover
    return out


def update_settings(
    *, slots_override: int | None = None, slot_coverage_pct: float | None = None,
    disaster_gap_pct: float | None = None,
) -> dict:
    """Operator's settings. None leaves a value as it is; 0 slots = calculated."""
    raw = kv_get(SETTINGS_KV_KEY, {})
    raw = dict(raw) if isinstance(raw, dict) else {}
    if slots_override is not None:
        if int(slots_override) < 0:
            raise ValueError("Slots must be 0 (calculated) or more.")
        raw["live_netting_slots_override"] = int(slots_override)
    if slot_coverage_pct is not None:
        if not 50.0 <= float(slot_coverage_pct) <= 100.0:
            raise ValueError("Slot coverage must be between 50 and 100 percent.")
        raw["live_netting_slot_coverage_pct"] = float(slot_coverage_pct)
    if disaster_gap_pct is not None:
        if not 0.0 <= float(disaster_gap_pct) <= 20.0:
            raise ValueError("The disaster stop gap must be between 0 and 20 percent.")
        raw["live_netting_disaster_gap_pct"] = float(disaster_gap_pct)
    kv_set(SETTINGS_KV_KEY, raw)
    return status()


# --- the books -------------------------------------------------------------------


def _armed_levels(sd: dict) -> tuple[float | None, float | None]:
    """(stop, target) Forven enforces for a row: the same levels the mark watcher arms."""
    def pos(value: object) -> float | None:
        number = _float(value)
        return number if number is not None and number > 0 else None

    stop = pos(sd.get("stop_loss_price")) or pos(sd.get("stop_loss"))
    manual = str(sd.get("stop_loss_source") or "").strip().lower() == "manual"
    if sd.get("kernel_managed") and not manual and not sd.get("late_entry"):
        effective = pos(sd.get("effective_stop_price"))
        if effective is not None:
            stop = effective
    target = pos(sd.get("take_profit_price")) or pos(sd.get("take_profit"))
    return stop, target


def open_rows(coin: str | None = None) -> list[dict]:
    """OPEN live trades on the master wallet: the rows the exchange position is the sum of."""
    from forven.trade_state import is_local_only_paper_trade, parse_trade_signal_data

    with get_db() as conn:
        raw = conn.execute(
            "SELECT * FROM trades WHERE status = 'OPEN' "
            "AND LOWER(COALESCE(execution_type, 'live')) = 'live'"
        ).fetchall()
    out: list[dict] = []
    wanted = coin_of(coin) if coin else None
    for item in raw:
        trade = dict(item)
        book = str(trade.get("book") or "").strip().lower()
        if book not in ("", "main"):
            continue  # routed to a sub-account or a named wallet: not the engine's
        if is_local_only_paper_trade(trade):
            continue
        row_coin = coin_of(trade.get("asset"))
        if not row_coin or (wanted and row_coin != wanted):
            continue
        size = abs(_float(trade.get("size")) or 0.0)
        if size <= 0:
            continue
        sd = parse_trade_signal_data(trade.get("signal_data"))
        stop, target = _armed_levels(sd)
        side = -1 if str(trade.get("direction") or "long").strip().lower() == "short" else 1
        entry = (
            _float(trade.get("fill_entry_price")) or _float(trade.get("entry_price"))
            or _float(trade.get("signal_entry_price"))
        )
        out.append({
            "trade_id": str(trade.get("id")),
            "strategy_id": str(trade.get("strategy_id") or trade.get("strategy") or ""),
            "coin": row_coin,
            "side": side,
            "size": size,
            "entry_price": entry,
            "stop_price": stop,
            "target_price": target,
            "leverage": _float(trade.get("leverage")) or 1.0,
            "exchange_leverage": _float(sd.get("exchange_leverage")),
            "opened_at": trade.get("opened_at"),
            "signal_data": sd,
        })
    return out


def book_net(coin: str, rows: list[dict] | None = None) -> float:
    rows = open_rows(coin) if rows is None else [r for r in rows if r["coin"] == coin_of(coin)]
    return sum(r["side"] * r["size"] for r in rows)


def expected_position(coin: str, rows: list[dict] | None = None) -> float:
    """What the exchange should hold now: the books, less closes that have filled
    but whose row the caller has not closed yet."""
    coin = coin_of(coin)
    rows = open_rows(coin) if rows is None else [r for r in rows if r["coin"] == coin]
    net = sum(r["side"] * r["size"] for r in rows)
    open_ids = {r["trade_id"] for r in rows}
    now = time.time()
    for trade_id, (fill_coin, units, at) in list(_filled_closes.items()):
        if now - at > CLOSE_CLAIM_SECONDS or (fill_coin == coin and trade_id not in open_ids):
            _filled_closes.pop(trade_id, None)
        elif fill_coin == coin:
            net -= units
    return net


def coin_leverage(coin: str) -> float:
    """Highest exchange leverage any open row on the coin was sized with."""
    return max((r["exchange_leverage"] or 0.0 for r in open_rows(coin)), default=0.0)


def disaster_level(rows: list[dict], net_units: float, gap_pct: float) -> tuple[str, float] | None:
    """(position side, trigger price): just beyond the furthest stop on the net side."""
    gap = max(gap_pct, 0.0) / 100.0
    if _same(net_units, 0.0):
        return None
    side = 1 if net_units > 0 else -1
    stops: list[float] = []
    for row in rows:
        if row["side"] != side:
            continue
        stop = row.get("stop_price")
        if not stop and row.get("entry_price"):
            stop = row["entry_price"] * (1.0 - side * NO_STOP_FALLBACK_FRAC)
        if stop:
            stops.append(float(stop))
    if not stops:
        return None
    if side > 0:
        return "long", min(stops) * (1.0 - gap)
    return "short", max(stops) * (1.0 + gap)


# --- reading the exchange --------------------------------------------------------


def _parse_positions(payload: object) -> dict[str, float]:
    raw = payload.get("positions") if isinstance(payload, dict) else payload
    out: dict[str, float] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        pos = item.get("position", item)
        if not isinstance(pos, dict):
            continue
        coin = coin_of(pos.get("coin") or pos.get("asset"))
        if "szi" in pos:
            size = _float(pos.get("szi"))
        else:  # already normalized by risk._normalize_exchange_positions
            size = _float(pos.get("size"))
            if size is not None and str(pos.get("direction") or "").strip().lower() == "short":
                size = -abs(size)
        if coin and size:
            out[coin] = out.get(coin, 0.0) + size
    return out


def exchange_positions(testnet: bool | None = None) -> dict[str, float]:
    """Signed net position per coin on the master wallet. Raises when unreadable."""
    from forven.exchange.hyperliquid import get_positions

    return _parse_positions(get_positions(testnet=_testnet() if testnet is None else testnet))


def _open_order_ids(open_orders: list | None, coin: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for order in open_orders if isinstance(open_orders, list) else []:
        if not isinstance(order, dict) or coin_of(order.get("coin") or order.get("asset")) != coin:
            continue
        oid = order.get("oid") or order.get("orderId") or order.get("order_id")
        if oid is not None:
            out[str(oid)] = order
    return out


# --- mismatch state --------------------------------------------------------------


def mismatches(*, confirmed_only: bool = False) -> dict[str, dict]:
    _ensure_schema()
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM live_netting_mismatch").fetchall()
    out = {}
    for row in rows:
        item = dict(row)
        item["confirmed"] = int(item.get("seen") or 0) >= MISMATCH_CONFIRM_SWEEPS
        if confirmed_only and not item["confirmed"]:
            continue
        out[str(item["coin"])] = item
    return out


def coin_block_reason(coin: str) -> str | None:
    """Why no new entry may be taken on this coin now, or None."""
    item = mismatches(confirmed_only=True).get(coin_of(coin))
    if not item:
        return None
    return (
        f"the books and the exchange disagree on {coin_of(coin)} (books {item['book_units']:g}, "
        f"exchange {item['exchange_units']:g}). New entries on it are paused until that is resolved "
        "on the Live netting page."
    )


def _record_mismatch(coin: str, book: float, held: float, note: str | None = None) -> int:
    _ensure_schema()
    now = _iso()
    with get_db() as conn:
        row = conn.execute("SELECT seen FROM live_netting_mismatch WHERE coin = ?", (coin,)).fetchone()
        seen = int(row["seen"]) + 1 if row else 1
        conn.execute(
            "INSERT INTO live_netting_mismatch (coin, book_units, exchange_units, seen, first_seen, last_seen, note) "
            "VALUES (?, ?, ?, 1, ?, ?, ?) "
            "ON CONFLICT(coin) DO UPDATE SET book_units = excluded.book_units, "
            "exchange_units = excluded.exchange_units, seen = live_netting_mismatch.seen + 1, "
            "last_seen = excluded.last_seen, note = excluded.note",
            (coin, book, held, now, now, note),
        )
    return seen


def _clear_mismatch(coin: str) -> None:
    _ensure_schema()
    with get_db() as conn:
        conn.execute("DELETE FROM live_netting_mismatch WHERE coin = ?", (coin,))


# --- one close per trade ---------------------------------------------------------
#
# A reduce-only close cannot be sent twice by mistake: the second finds nothing
# to reduce. On a shared position it can, and it would sell another strategy's
# part. The mark watcher, the scanner and an operator's manual close can all
# reach the same trade within a second of each other, from different processes,
# so the claim lives in the database.


def _claim_close(trade_id: str) -> str | None:
    """Reserve the right to close this trade. Returns why not, or None when claimed."""
    from forven.db import get_db_immediate

    _ensure_schema()
    now = time.time()
    with get_db_immediate() as conn:
        row = conn.execute("SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if row is not None and str(row["status"] or "").strip().upper() != "OPEN":
            return "trade is already closed"
        claim = conn.execute("SELECT at FROM live_netting_closing WHERE trade_id = ?", (trade_id,)).fetchone()
        if claim is not None and now - float(claim["at"]) < CLOSE_CLAIM_SECONDS:
            return "a close for this trade was just sent"
        conn.execute(
            "INSERT INTO live_netting_closing (trade_id, at) VALUES (?, ?) "
            "ON CONFLICT(trade_id) DO UPDATE SET at = excluded.at",
            (trade_id, now),
        )
        conn.execute("DELETE FROM live_netting_closing WHERE at < ?", (now - 3600.0,))
    return None


def _release_close(trade_id: str) -> None:
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM live_netting_closing WHERE trade_id = ?", (trade_id,))
    except Exception as exc:  # noqa: BLE001 - the claim expires on its own
        log.debug("live netting: close claim for %s not released: %s", trade_id, exc)


# --- orders ----------------------------------------------------------------------


def _resolve_unknown_fill(result: dict, coin: str, held_before: float, signed_units: float,
                          testnet: bool, price_key: str) -> dict:
    """An order whose reply carried no fill price: read the position to see what filled.

    The legacy path parks such an order for the one-position-per-coin reconcile
    to work out later. Here the position is shared, so it is settled at once:
    the change in the exchange position is what filled, priced at the mid the
    order was sent against (and flagged as an estimate).
    """
    try:
        held_after = exchange_positions(testnet).get(coin, 0.0)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"order sent on {coin} but its fill could not be confirmed ({exc})"}
    moved = held_after - held_before
    filled = abs(moved) if moved * signed_units > 0 else 0.0
    if filled <= max(1e-9, abs(signed_units) * 1e-6):
        return {"error": f"order on {coin} did not fill"}
    out = dict(result)
    out.pop("fill_price_unknown", None)
    out[price_key] = out.get("mid")
    out["filled_size"] = min(filled, abs(signed_units))
    out["netting_fill_price_estimated"] = True
    return out


def market_order(
    asset: str, side: str, size: float,
    stop_loss_price: float | None = None,
    take_profit_price: float | None = None,
    idempotency_key: str | None = None,
    testnet: bool = True,
    vault_address: str | None = None,
) -> dict:
    """Entry order. Same call as ``hyperliquid.market_order``.

    Through the engine the order carries no stop or target of its own: those
    would be sized for one strategy but act on the shared position. Forven runs
    them, and the coin's disaster stop is re-set straight after the fill.
    """
    from forven.exchange import hyperliquid as hl

    if not active_for(vault_address):
        kwargs: dict = {
            "stop_loss_price": stop_loss_price, "take_profit_price": take_profit_price,
            "idempotency_key": idempotency_key, "testnet": testnet,
        }
        if vault_address:
            kwargs["vault_address"] = vault_address
        return hl.market_order(asset, side, size, **kwargs)

    coin = coin_of(asset)
    blocked = coin_block_reason(coin)
    if blocked:
        return {"error": f"entry skipped: {blocked}"}
    is_buy = str(side).strip().upper() in ("B", "BUY", "LONG")
    signed = abs(float(size)) * (1 if is_buy else -1)
    with _lock(coin):
        try:
            held_before = exchange_positions(testnet).get(coin, 0.0)
            result = hl.market_order(coin, side, abs(float(size)), idempotency_key=idempotency_key, testnet=testnet)
            if isinstance(result, dict) and not result.get("error") and result.get("fill_price_unknown"):
                result = _resolve_unknown_fill(result, coin, held_before, signed, testnet, "entry_price")
        finally:
            _last_order_at[coin] = time.time()
    if isinstance(result, dict) and not result.get("error"):
        result["netting"] = True
    return result


def close_position(
    asset: str, size: float, side: str = "sell", testnet: bool = True,
    vault_address: str | None = None, *, slippage_bps: float | None = None,
    trade_id: str | None = None,
) -> dict:
    """Exit order for ONE strategy's position. Same call as ``hyperliquid.close_position``.

    ``side`` is the order side, so "sell" takes out a long. When the exchange
    holds at least that much on the strategy's side, this is the usual
    reduce-only close. When it holds less, other strategies' opposite positions
    are netted against this one, and taking it out means the exchange position
    must move through zero: a plain order then, sent only while the books and
    the exchange agree. If they are known to disagree, only what really is there
    is reduced and the rest is closed in the books alone, so the engine never
    opens exposure the books cannot account for.
    """
    from forven.exchange import hyperliquid as hl

    if not active_for(vault_address):
        kwargs: dict = {"testnet": testnet}
        if vault_address:
            kwargs["vault_address"] = vault_address
        if slippage_bps is not None:
            kwargs["slippage_bps"] = slippage_bps
        return hl.close_position(asset, size, side, **kwargs)

    coin = coin_of(asset)
    size = abs(float(size or 0.0))
    if size <= 0:
        return {"error": f"close size for {coin} is non-positive"}
    is_buy = str(side).strip().lower() in ("b", "buy")
    row_side = -1 if is_buy else 1  # buying closes a short
    close_kwargs: dict = {"testnet": testnet}
    if slippage_bps is not None:
        close_kwargs["slippage_bps"] = slippage_bps

    claimed = str(trade_id or "").strip() or None
    if claimed:
        refused = _claim_close(claimed)
        if refused:
            return {"error": f"{coin}: {refused}; not sending another close", "netting_close_refused": True}
    try:
        result = _close_locked(coin, size, side, row_side, testnet, close_kwargs, claimed)
    except Exception:
        if claimed:
            _release_close(claimed)
        raise
    if claimed and (
        not isinstance(result, dict) or result.get("error")
        or (_float(result.get("filled_size")) or 0.0) + max(1e-9, size * 1e-6) < size
    ):
        _release_close(claimed)  # nothing, or only part, was closed: a retry is right
    return result


def _close_locked(coin: str, size: float, side: str, row_side: int, testnet: bool, close_kwargs: dict,
                  trade_id: str | None = None) -> dict:
    from forven.exchange import hyperliquid as hl

    with _lock(coin):
        try:
            held = exchange_positions(testnet).get(coin, 0.0)
            same_side = held * row_side
            if same_side >= size or _same(same_side, size):
                result = hl.close_position(coin, size, side, **close_kwargs)
                mode = "reduce"
            else:
                books = expected_position(coin)
                if _same(held, books):
                    raw = hl.market_order(
                        coin, side, size, idempotency_key=f"netting-close:{coin}:{time.time_ns()}", testnet=testnet,
                    )
                    result = _close_shape(raw, coin, held, -row_side * size, testnet)
                    mode = "through_zero"
                elif coin in mismatches(confirmed_only=True):
                    reducible = max(min(same_side, size), 0.0)
                    if reducible > max(1e-9, size * 1e-6):
                        result = hl.close_position(coin, reducible, side, **close_kwargs)
                        mode = "reduce_available"
                    else:
                        mid = _float(hl.get_all_mids(testnet).get(coin))
                        if not mid:
                            return {"error": f"Could not get mid price for {coin}"}
                        result = {
                            "status": "ok", "mid": mid, "exit_price": mid, "close_price": mid,
                            "requested_size": size, "filled_size": size, "netting_unbacked_close": True,
                        }
                        mode = "books_only"
                        log.warning(
                            "live netting: %s close of %s booked without an order: the exchange holds %s, "
                            "nothing on that side to close", coin, size, held,
                        )
                else:
                    return {
                        "error": (
                            f"{coin}: exchange position {held:g} does not match the books {books:g} yet; "
                            "close held for retry"
                        )
                    }
            if isinstance(result, dict) and not result.get("error"):
                filled = _float(result.get("filled_size")) or 0.0
                if result.get("exit_price") is None and filled > 0:
                    result["exit_price"] = result.get("mid")
                    result["netting_fill_price_estimated"] = True
                result["netting"] = True
                result["netting_close_mode"] = mode
                # Only a FULL fill: after a partial one the caller shrinks the row
                # itself, so the books already show what is left.
                if trade_id and mode != "books_only" and filled + max(1e-9, size * 1e-6) >= size:
                    _filled_closes[trade_id] = (coin, row_side * size, time.time())
            return result
        finally:
            _last_order_at[coin] = time.time()


def _close_shape(raw: object, coin: str, held_before: float, signed_units: float, testnet: bool) -> dict:
    """Give a plain order's reply the shape of a ``close_position`` reply."""
    if not isinstance(raw, dict):
        return {"error": f"close order on {coin} returned no data"}
    if raw.get("error"):
        return raw
    if raw.get("fill_price_unknown"):
        raw = _resolve_unknown_fill(raw, coin, held_before, signed_units, testnet, "entry_price")
        if raw.get("error"):
            return raw
    oid = raw.get("entry_order_id") or raw.get("order_id")
    out = {
        "status": raw.get("status", "ok"),
        "mid": raw.get("mid"),
        "exit_price": raw.get("entry_price"),
        "close_price": raw.get("entry_price"),
        "requested_size": raw.get("requested_size"),
        "filled_size": raw.get("filled_size"),
    }
    if oid is not None:
        out["order_id"] = oid
        out["exit_order_id"] = oid
    if raw.get("netting_fill_price_estimated"):
        out["netting_fill_price_estimated"] = True
    return out


# --- the disaster stop -----------------------------------------------------------


def _tracked_stops() -> dict[str, dict]:
    _ensure_schema()
    with get_db() as conn:
        return {str(r["coin"]): dict(r) for r in conn.execute("SELECT * FROM live_netting_stops").fetchall()}


def disaster_stop_oids(coin: str | None = None) -> set[str]:
    """Order ids of the engine's resting disaster stops (never to be swept as strays)."""
    try:
        tracked = _tracked_stops()
    except Exception:  # noqa: BLE001
        return set()
    wanted = coin_of(coin) if coin else None
    return {
        str(item["oid"]) for key, item in tracked.items()
        if item.get("oid") and (wanted is None or key == wanted)
    }


def _save_stop(coin: str, *, oid: str | None, direction: str, size: float, price: float, error: str | None) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO live_netting_stops (coin, oid, direction, size, price, placed_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(coin) DO UPDATE SET oid = excluded.oid, direction = excluded.direction, "
            "size = excluded.size, price = excluded.price, placed_at = excluded.placed_at, error = excluded.error",
            (coin, oid, direction, size, price, _iso(), error),
        )


def _cancel_stop(coin: str, oid: object, testnet: bool) -> None:
    from forven.exchange.hyperliquid import cancel_order

    try:
        cancel_order(coin, int(str(oid)), testnet=testnet, protective_cleanup=True)
    except Exception as exc:  # noqa: BLE001 - already filled or gone is fine
        log.info("live netting: old disaster stop %s on %s not cancelled (%s)", oid, coin, exc)


def refresh_disaster_stop(coin: str, *, testnet: bool | None = None, open_order_ids: dict | None = None) -> str:
    """Make the coin's resting disaster stop match the books. Returns what was done.

    Place-before-cancel: the old stop stays until the new one is accepted, so
    the position is never left without one by a rejected replacement.
    """
    from forven.exchange.hyperliquid import place_protective_stop

    coin = coin_of(coin)
    testnet = _testnet() if testnet is None else testnet
    _ensure_schema()
    with _lock(coin):
        rows = open_rows(coin)
        net = book_net(coin, rows)
        want = disaster_level(rows, net, settings()["live_netting_disaster_gap_pct"])
        current = _tracked_stops().get(coin)
        current_oid = str(current["oid"]) if current and current.get("oid") else None

        if want is None:
            if current:
                if current_oid:
                    _cancel_stop(coin, current_oid, testnet)
                with get_db() as conn:
                    conn.execute("DELETE FROM live_netting_stops WHERE coin = ?", (coin,))
                return "removed"
            return "none"

        direction, price = want
        size = abs(net)
        still_resting = open_order_ids is None or (current_oid is not None and current_oid in open_order_ids)
        if (
            current and current_oid and still_resting and current["direction"] == direction
            and _same(float(current["size"]), size)
            and abs(float(current["price"]) - price) <= abs(price) * 1e-5
        ):
            return "kept"

        try:
            result = place_protective_stop(coin, direction, size, price, testnet=testnet)
        except Exception as exc:  # noqa: BLE001
            result = {"error": str(exc)}
        new_oid = result.get("stop_order_id") if isinstance(result, dict) else None
        if not new_oid or (isinstance(result, dict) and result.get("error")):
            error = str((result or {}).get("error") if isinstance(result, dict) else result)[:300]
            log.error("live netting: disaster stop for %s could not be set (%s)", coin, error)
            if current:
                with get_db() as conn:
                    conn.execute("UPDATE live_netting_stops SET error = ? WHERE coin = ?", (error, coin))
            else:
                _save_stop(coin, oid=None, direction=direction, size=size, price=price, error=error)
            _notify(
                "live_netting_disaster_stop_failed", coin,
                f"Disaster stop not set ({coin})",
                f"The exchange refused the {coin} disaster stop at {price:.6g}: {error}. Forven still "
                "runs each strategy's own stop; it will try again within a minute.",
                severity="warning",
            )
            return "failed"
        _save_stop(coin, oid=str(new_oid), direction=direction, size=size, price=price, error=None)
        if current_oid and current_oid != str(new_oid):
            _cancel_stop(coin, current_oid, testnet)
        return "placed"


def after_fill(asset: str, *, testnet: bool | None = None) -> None:
    """Call after a live open or close is in the books. Never raises."""
    try:
        if enabled():
            refresh_disaster_stop(coin_of(asset), testnet=testnet)
    except Exception as exc:  # noqa: BLE001 - the periodic sweep retries
        log.warning("live netting: disaster stop refresh after fill failed for %s: %s", asset, exc)


# --- the periodic sweep ----------------------------------------------------------


def _fill_price_for_oid(oid: str, testnet: bool) -> float | None:
    try:
        from forven.exchange.hyperliquid import get_user_fills

        notional = units = 0.0
        for fill in get_user_fills(testnet=testnet):
            if str(fill.get("oid")) != str(oid):
                continue
            px, sz = _float(fill.get("px")), _float(fill.get("sz"))
            if px and sz:
                notional += px * sz
                units += sz
        return notional / units if units > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _close_rows_in_books(rows: list[dict], price: float | None, reason: str, source: str, extra: dict) -> list[str]:
    from forven.exchange.risk import release
    from forven.trade_state import close_trade_record

    closed: list[str] = []
    for row in rows:
        result = close_trade_record(
            row["trade_id"], signal_exit_price=price, exit_price=price, close_reason=reason,
            close_price_source=source if price is not None else "missing_price",
            extra_signal_data=extra,
        )
        if result and result.get("updated"):
            closed.append(row["trade_id"])
            try:
                release(row["trade_id"])
            except Exception:  # noqa: BLE001
                pass
    return closed


def _handle_disaster_fired(coin: str, rows: list[dict], tracked: dict, testnet: bool) -> list[str]:
    """The disaster stop closed the whole position: close every row at its fill.

    Selling the net of 3 long and 1 short is the same trade as closing all four
    at that price, so booking each row out at the fill is exact.
    """
    price = _fill_price_for_oid(str(tracked.get("oid")), testnet) or _float(tracked.get("price"))
    closed = _close_rows_in_books(
        rows, price, "disaster_stop", "netting_disaster_stop",
        {"netting_disaster_stop": True, "netting_disaster_stop_oid": tracked.get("oid")},
    )
    with get_db() as conn:
        conn.execute("DELETE FROM live_netting_stops WHERE coin = ?", (coin,))
    _clear_mismatch(coin)
    _notify(
        "live_netting_disaster_stop_fired", f"{coin}:{tracked.get('oid')}",
        f"Disaster stop fired ({coin})",
        f"The exchange closed the whole {coin} position at about {price:.6g}. Forven was not able to "
        f"close the strategies' own stops first. {len(closed)} live trade(s) were closed in the books "
        "at that price.",
    )
    return closed


def _clear_stale_pending_closes(rows: list[dict]) -> None:
    """A close marked 'pending' that did not happen: free it so the close is retried."""
    cutoff = _now() - timedelta(seconds=PENDING_CLOSE_GRACE_SECONDS)
    for row in rows:
        sd = row["signal_data"]
        if not sd.get("pending_close_reconcile"):
            continue
        try:
            marked = datetime.fromisoformat(str(sd.get("pending_close_reconcile_at")).replace("Z", "+00:00"))
            if marked.tzinfo is None:
                marked = marked.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            marked = None
        if marked is not None and marked > cutoff:
            continue
        try:
            from forven.scanner import _update_trade_signal_data

            _update_trade_signal_data(row["trade_id"], {
                "pending_close_reconcile": False, "netting_pending_close_cleared_at": _iso(),
            })
        except Exception as exc:  # noqa: BLE001
            log.warning("live netting: could not clear pending close on %s: %s", row["trade_id"], exc)


def sweep(*, positions: dict[str, float] | None = None, open_orders: list | None = None,
          testnet: bool | None = None) -> dict:
    """Check every coin's books against the exchange and keep the disaster stops right.

    Reads only, plus protective-stop upkeep. A difference is recorded and, once
    confirmed on a second sweep, blocks new entries on that coin and alerts the
    operator. The one case settled here is a fired disaster stop.
    """
    if not enabled():
        return {}
    _ensure_schema()
    rows = open_rows()
    tracked = _tracked_stops()
    known = mismatches()
    if not rows and not tracked and not known and positions is None:
        return {}

    testnet = _testnet() if testnet is None else testnet
    if positions is None:
        positions = exchange_positions(testnet)
    if open_orders is None:
        from forven.exchange.hyperliquid import get_open_orders

        open_orders = get_open_orders(testnet=testnet)

    by_coin: dict[str, list[dict]] = {}
    for row in rows:
        by_coin.setdefault(row["coin"], []).append(row)

    out: dict = {"coins": 0, "mismatched": [], "confirmed": [], "disaster_fired": [], "stops": {}}
    now = time.time()
    for coin in sorted(set(by_coin) | set(tracked) | set(known) | set(positions)):
        out["coins"] += 1
        if now - _last_order_at.get(coin, 0.0) < ORDER_SETTLE_SECONDS:
            continue  # an order of ours just went through: let the read catch up
        coin_rows = by_coin.get(coin, [])
        books = expected_position(coin, coin_rows)
        held = float(positions.get(coin, 0.0))
        order_ids = _open_order_ids(open_orders, coin)

        if _same(books, held):
            if coin in known:
                _clear_mismatch(coin)
            _clear_stale_pending_closes(coin_rows)
            try:
                out["stops"][coin] = refresh_disaster_stop(coin, testnet=testnet, open_order_ids=order_ids)
            except Exception as exc:  # noqa: BLE001
                out["stops"][coin] = f"error: {exc}"
            continue

        stop = tracked.get(coin)
        stop_gone = bool(stop and stop.get("oid") and str(stop["oid"]) not in order_ids)
        seen = _record_mismatch(coin, books, held, "disaster stop no longer resting" if stop_gone else None)
        out["mismatched"].append(coin)
        if seen < MISMATCH_CONFIRM_SWEEPS:
            continue
        if stop_gone and _same(held, 0.0) and coin_rows:
            _handle_disaster_fired(coin, coin_rows, stop, testnet)
            out["disaster_fired"].append(coin)
            continue
        out["confirmed"].append(coin)
        if seen == MISMATCH_CONFIRM_SWEEPS:
            _notify(
                "live_netting_mismatch", coin,
                f"Books and exchange disagree ({coin})",
                f"Forven's live trades on {coin} add up to {books:g} but the exchange holds {held:g}. "
                f"New entries on {coin} are paused. Open the Live netting page to resolve it.",
            )
    try:
        kv_set(SWEEP_KV_KEY, {"at": _iso(), "positions": positions, **{k: v for k, v in out.items() if k != "stops"}})
    except Exception:  # noqa: BLE001
        pass
    return out


def reconcile(snapshot: dict, *, testnet: bool) -> dict:
    """The reconcile pass for the master wallet while the engine is on.

    The one-trade-per-position matcher cannot be used: with shared coins a
    trade has no exchange position of its own to be matched to. The check is
    the net position per coin; confirmed differences are returned as
    discrepancies, which is what pauses new entries until an operator acts.
    """
    positions = _parse_positions(snapshot.get("positions") or snapshot.get("raw_positions") or [])
    sweep(positions=positions, open_orders=list(snapshot.get("open_orders") or []), testnet=testnet)
    discrepancies = [
        {
            "type": "netting_position_mismatch",
            "details": (
                f"{coin}: live trades add up to {item['book_units']:g} but the exchange holds "
                f"{item['exchange_units']:g}"
            ),
        }
        for coin, item in sorted(mismatches(confirmed_only=True).items())
    ]
    return {
        "sqlite_open": len(open_rows()),
        "exchange_open": len([c for c, v in positions.items() if not _same(v, 0.0)]),
        "discrepancies": discrepancies,
        "informational": [],
        "adopted_positions": [],
        "adopted_count": 0,
        "resolved_actions": [],
        "synced": not discrepancies,
        "testnet": bool(testnet),
        "netting": True,
    }


def close_offset_rows_after_flatten(prices: dict[str, float] | None = None) -> list[str]:
    """After the kill switch flattened the account: close rows no position backs.

    The flatten closes every row on a coin the exchange held. Rows that offset
    each other exactly (the exchange held nothing) are left over; with trading
    halted they are closed in the books at the current price.
    """
    if not enabled():
        return []
    try:
        positions = exchange_positions()
    except Exception as exc:  # noqa: BLE001
        log.error("live netting: could not read positions after the flatten: %s", exc)
        return []
    if prices is None:
        try:
            from forven.exchange.hyperliquid import get_all_mids

            prices = get_all_mids(_testnet())
        except Exception:  # noqa: BLE001
            prices = {}
    closed: list[str] = []
    by_coin: dict[str, list[dict]] = {}
    for row in open_rows():
        by_coin.setdefault(row["coin"], []).append(row)
    for coin, rows in by_coin.items():
        if not _same(float(positions.get(coin, 0.0)), 0.0):
            continue
        price = _float((prices or {}).get(coin))
        closed += _close_rows_in_books(rows, price, "kill_switch", "kill_switch_netting_offset",
                                       {"netting_offset_close": True})
        try:
            refresh_disaster_stop(coin)
        except Exception:  # noqa: BLE001
            pass
    return closed


# --- operator tools --------------------------------------------------------------


def resolve_mismatch(coin: str, action: str) -> dict:
    """Operator's choice for a coin whose books and exchange position differ.

    ``trade_to_books``: send the order that makes the exchange hold what the
    books say. ``close_books``: the exchange is right and holds nothing, so
    close the coin's live trades in the books at the current price, no order.
    """
    from forven.exchange import hyperliquid as hl

    coin = coin_of(coin)
    action = str(action or "").strip().lower()
    if not enabled():
        raise ValueError("The netting engine is off.")
    testnet = _testnet()
    with _lock(coin):
        rows = open_rows(coin)
        books = book_net(coin, rows)
        held = exchange_positions(testnet).get(coin, 0.0)
        mid = _float(hl.get_all_mids(testnet).get(coin))
        if _same(books, held):
            _clear_mismatch(coin)
            refresh_disaster_stop(coin, testnet=testnet)
            return {"ok": True, "action": "none", "detail": f"{coin} already matches."}
        if action == "close_books":
            if not _same(held, 0.0):
                raise ValueError(
                    f"The exchange still holds {held:g} {coin}. Close it on the exchange first, or "
                    "choose to trade the exchange to the books."
                )
            closed = _close_rows_in_books(rows, mid, "netting_books_closed", "netting_operator",
                                          {"netting_operator_close": True})
            _clear_mismatch(coin)
            refresh_disaster_stop(coin, testnet=testnet)
            return {"ok": True, "action": action, "closed_trades": closed}
        if action != "trade_to_books":
            raise ValueError("action must be 'trade_to_books' or 'close_books'")
        if not mid:
            raise ValueError(f"No price for {coin}.")
        delta = books - held
        if abs(delta) * mid + 1e-9 < MIN_ORDER_USD:
            raise ValueError(
                f"The difference ({abs(delta):g} {coin}, about ${abs(delta) * mid:.2f}) is under the "
                f"exchange's ${MIN_ORDER_USD:.0f} minimum order."
            )
        side = "buy" if delta > 0 else "sell"
        results = []
        try:
            reducing = min(abs(delta), abs(held)) if held * delta < 0 else 0.0
            if reducing > 0:
                results.append(hl.close_position(coin, reducing, side, testnet=testnet))
            rest = abs(delta) - reducing
            if rest * mid + 1e-9 >= MIN_ORDER_USD or (rest > 0 and reducing == 0):
                results.append(hl.market_order(
                    coin, side, rest, idempotency_key=f"netting-resolve:{coin}:{time.time_ns()}", testnet=testnet,
                ))
        finally:
            _last_order_at[coin] = time.time()
        errors = [str(r.get("error")) for r in results if isinstance(r, dict) and r.get("error")]
        if errors:
            raise ValueError("; ".join(errors))
        _clear_mismatch(coin)
        _last_order_at[coin] = 0.0
        refresh_disaster_stop(coin, testnet=testnet)
        return {"ok": True, "action": action, "orders": len(results), "delta_units": delta}


# --- slots -----------------------------------------------------------------------


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _cohort_weights() -> dict[str, float] | None:
    """Live strategy -> share of a slot it trades at (probation size, else 1.0)."""
    from forven.exchange.risk import live_cohort_ids
    from forven.netting_shadow import size_fractions

    cohort = live_cohort_ids()
    if cohort is None:
        return None
    with get_db() as conn:
        return size_fractions(conn, set(cohort), 100.0)


def _history_intervals(weights: dict[str, float]) -> list[tuple]:
    if not weights:
        return []
    now = _now()
    since = (now - timedelta(days=HISTORY_DAYS)).isoformat()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT COALESCE(strategy_id, strategy) AS sid, opened_at, closed_at, status "
            "FROM trades WHERE opened_at >= ?",
            (since,),
        ).fetchall()
    out = []
    for row in rows:
        sid = str(row["sid"] or "")
        if sid not in weights:
            continue
        start = _parse_ts(row["opened_at"])
        end = _parse_ts(row["closed_at"]) if str(row["status"] or "").upper() != "OPEN" else now
        if start is None or end is None or end <= start:
            continue
        out.append((start, end, weights[sid]))
    return out


def slot_state(equity: float | None, *, refresh: bool = False) -> dict | None:
    """The slot plan for this install's balance. None when it cannot be worked out.

    The count is recalculated when the live cohort or a strategy's size changes,
    at most hourly otherwise. It never resizes a trade that is already open.
    """
    from forven.netting_shadow import slot_plan

    eq = _float(equity)
    if eq is None or eq <= 0:
        return None
    weights = _cohort_weights()
    if weights is None:
        return None
    cfg = settings()
    signature = sorted((sid, round(w, 4)) for sid, w in weights.items())
    cached = kv_get(SLOTS_KV_KEY, None)
    fresh = (
        isinstance(cached, dict) and not refresh
        and cached.get("signature") == [list(item) for item in signature]
        and cached.get("coverage_pct") == cfg["live_netting_slot_coverage_pct"]
        and time.time() - float(cached.get("computed_ts") or 0.0) < SLOT_REFRESH_SECONDS
        and abs(float(cached.get("equity_usd") or 0.0) - eq) <= 0.10 * eq
    )
    if fresh:
        plan = dict(cached)
    else:
        smallest = min(weights.values(), default=1.0) or 1.0
        plan = slot_plan(
            eq, _history_intervals(weights),
            coverage_pct=cfg["live_netting_slot_coverage_pct"],
            # Leverage 1 for the ceiling: an order at the smallest size in use must
            # clear the exchange minimum even for a strategy that uses no leverage.
            leverage=1.0, smallest_size_pct=smallest * 100.0,
        )
        plan.update(
            signature=[list(item) for item in signature], coverage_pct=cfg["live_netting_slot_coverage_pct"],
            computed_ts=time.time(), computed_at=_iso(), equity_usd=round(eq, 2),
            cohort_size=len(weights), smallest_size_pct=round(smallest * 100.0, 2),
        )
        try:
            kv_set(SLOTS_KV_KEY, plan)
        except Exception:  # noqa: BLE001
            pass
    override = int(cfg["live_netting_slots_override"] or 0)
    if override > 0:
        plan["slots_calculated"] = plan["slots"]
        plan["slots"] = override
        plan["override"] = True
    plan["slot_usd"] = round(eq / max(int(plan["slots"]), 1), 4)
    plan["weights"] = weights
    return plan


def slot_slice(equity: float | None) -> tuple[float | None, dict]:
    """Sizing base for one live order: one slot. Same contract as ``risk.live_equity_slice``."""
    meta: dict = {"cohort_size": None, "account_equity_usd": None, "slice_usd": None, "netting": True}
    eq = _float(equity)
    if eq is None or eq <= 0:
        return None, {**meta, "reason": "account equity unavailable"}
    try:
        plan = slot_state(eq)
    except Exception as exc:  # noqa: BLE001 - fail closed, never guess a size
        return None, {**meta, "account_equity_usd": round(eq, 2), "reason": f"slot plan failed: {exc}"}
    if plan is None:
        return None, {**meta, "account_equity_usd": round(eq, 2), "reason": "live cohort unreadable"}
    return float(plan["slot_usd"]), {
        "cohort_size": plan.get("cohort_size"),
        "account_equity_usd": round(eq, 2),
        "slice_usd": plan["slot_usd"],
        "slots": plan["slots"],
        "slots_wanted": plan.get("slots_wanted"),
        "slots_capital_cap": plan.get("slots_capital_cap"),
        "netting": True,
    }


def slot_share(row: dict, weights: dict[str, float] | None = None) -> float:
    """How much of one slot an open trade takes: 1 at full size, its share on probation."""
    probation = row["signal_data"].get("probation")
    size_pct = _float(probation.get("size_pct")) if isinstance(probation, dict) else None
    if size_pct is not None:
        return min(max(size_pct / 100.0, 0.0), 1.0)
    return (weights or {}).get(row["strategy_id"], 1.0) if isinstance(probation, dict) else 1.0


def slots_in_use(rows: list[dict] | None = None, weights: dict[str, float] | None = None) -> float:
    rows = open_rows() if rows is None else rows
    return sum(slot_share(row, weights) for row in rows)


def slot_admission(strategy_id: str, equity: float | None) -> tuple[bool, str]:
    """Whether a slot is free for this strategy's next entry."""
    try:
        plan = slot_state(equity)
    except Exception as exc:  # noqa: BLE001
        return False, f"slot plan unavailable ({exc}); live entry refused"
    if plan is None:
        return False, "slot plan unavailable (no live balance or cohort); live entry refused"
    weights = plan.get("weights") or {}
    need = float(weights.get(str(strategy_id), 1.0))
    used = slots_in_use(weights=weights)
    total = float(plan["slots"])
    if used + need > total + 1e-9:
        return False, (
            f"all live slots are in use ({used:g} of {total:g}; this entry needs {need:g}). "
            "It can trade when a live position closes."
        )
    return True, f"slot free ({used:g} of {total:g} in use)"


# --- status ----------------------------------------------------------------------


def status() -> dict:
    cfg = settings()
    out: dict = {
        "enabled": bool(cfg["live_netting_enabled"]),
        "active": enabled(),
        "books_on": _books_on(),
        "settings": cfg,
        "coins": [],
        "slots": None,
    }
    try:
        _ensure_schema()
        rows = open_rows()
        tracked = _tracked_stops()
        known = mismatches()
        last = kv_get(SWEEP_KV_KEY, None)
        last = last if isinstance(last, dict) else {}
        held = last.get("positions") if isinstance(last.get("positions"), dict) else {}
        out["last_sweep_at"] = last.get("at")
        by_coin: dict[str, list[dict]] = {}
        for row in rows:
            by_coin.setdefault(row["coin"], []).append(row)
        for coin in sorted(set(by_coin) | set(tracked) | set(known)):
            coin_rows = by_coin.get(coin, [])
            stop = tracked.get(coin)
            issue = known.get(coin)
            out["coins"].append({
                "coin": coin,
                "book_units": book_net(coin, coin_rows),
                "exchange_units": held.get(coin, 0.0) if out["last_sweep_at"] else None,
                "long_units": sum(r["size"] for r in coin_rows if r["side"] > 0),
                "short_units": sum(r["size"] for r in coin_rows if r["side"] < 0),
                "disaster_stop": (
                    {k: stop.get(k) for k in ("oid", "direction", "size", "price", "placed_at", "error")}
                    if stop else None
                ),
                "mismatch": (
                    {k: issue.get(k) for k in ("book_units", "exchange_units", "seen", "first_seen", "confirmed", "note")}
                    if issue else None
                ),
                "trades": [
                    {
                        **{k: r.get(k) for k in ("trade_id", "strategy_id", "side", "size", "entry_price", "stop_price", "target_price", "opened_at")},
                        "slot_share": slot_share(r),
                    }
                    for r in coin_rows
                ],
            })
        equity = None
        try:
            equity = _float((kv_get("daemon_state", {}) or {}).get("account_equity"))
        except Exception:  # noqa: BLE001
            equity = None
        plan = slot_state(equity) if equity else None
        if plan:
            used = slots_in_use(rows, plan.get("weights"))
            out["slots"] = {
                **{k: plan.get(k) for k in (
                    "slots", "slots_wanted", "slots_capital_cap", "capital_bound", "slot_usd", "override",
                    "slots_calculated", "cohort_size", "smallest_size_pct", "equity_usd", "computed_at",
                )},
                "in_use": used,
            }
    except Exception as exc:  # noqa: BLE001 - status must always answer
        out["error"] = str(exc)
    return out
