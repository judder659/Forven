"""Netting engine, shadow mode: what one shared live account would hold and send.

Plan stage 2 for the multi-strategy live engine. Nothing here places, cancels
or changes an order, and nothing here imports ``forven.exchange``. The shadow
follows the PAPER trades of the strategies that are live or ready for live and
keeps the books the real engine would keep:

* a **row** per strategy position (its own size, entry and stop), sized off one
  slot of the live account rather than the strategy's paper sandbox;
* the **net position** per coin, the sum of the rows, which is all the exchange
  would hold;
* the **order** for the difference whenever that sum changes, carried as a
  residual while it is under the exchange minimum;
* the **disaster stop** per coin, just beyond the furthest strategy stop on the
  net side;
* the **slot count**, from capital and from how many of these strategies have
  been in a trade at once.

Off by default (setting ``live_netting_shadow_enabled``). Turning it on changes
no trading behaviour on any install; it only records what would have happened so
it can be compared with what did.
"""

from __future__ import annotations

import json
import logging
import math
import time
from datetime import datetime, timedelta, timezone

from forven.db import get_db, kv_get, kv_set

log = logging.getLogger("forven.netting_shadow")

STATE_KV_KEY = "forven:netting_shadow:state"
READY_KV_KEY = "forven:probation:ready"
SETTINGS_KV_KEY = "forven:settings"

PAPER_TYPES = ("paper", "paper_challenger", "simulation")
LIVE_STAGES = ("live_graduated", "deployed")

DEFAULTS = {
    "live_netting_shadow_enabled": False,
    # Share of time the slots must cover; the rest is when an entry is blocked.
    "live_netting_slot_coverage_pct": 95.0,
    # The disaster stop sits this far beyond the furthest strategy stop.
    "live_netting_disaster_gap_pct": 0.5,
    # Size, as a share of one slot, of a strategy that is ready but not live yet:
    # the size it would start probation at. Live strategies use their real size.
    "live_netting_shadow_size_pct": 25.0,
    "live_netting_leverage": 2.0,
    # Optional stand-in balance for an install with no live account to read.
    # 0 means none: without a real balance the shadow has nothing to size from
    # and stays idle rather than assume a figure.
    "live_netting_shadow_equity_usd": 0.0,
}
MIN_ORDER_USD = 10.0
TAKER_FEE_FRAC = 0.00045
HISTORY_DAYS = 180
SLOT_REFRESH_SECONDS = 3600.0
_EPS = 1e-12

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS netting_shadow_rows (
    trade_id     TEXT PRIMARY KEY,
    strategy_id  TEXT NOT NULL,
    coin         TEXT NOT NULL,
    side         INTEGER NOT NULL,
    units        REAL NOT NULL,
    size_frac    REAL NOT NULL DEFAULT 1.0,
    entry_price  REAL,
    stop_price   REAL,
    status       TEXT NOT NULL,
    reason       TEXT,
    opened_at    TEXT,
    closed_at    TEXT,
    exit_price   REAL,
    pnl_usd      REAL
);
CREATE INDEX IF NOT EXISTS idx_netting_shadow_rows_status ON netting_shadow_rows (status);
CREATE TABLE IF NOT EXISTS netting_shadow_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    coin         TEXT NOT NULL,
    delta_units  REAL NOT NULL,
    price        REAL NOT NULL,
    notional_usd REAL NOT NULL,
    fee_usd      REAL NOT NULL,
    net_after    REAL NOT NULL,
    strategies   TEXT
);
"""


# --- pure arithmetic ------------------------------------------------------------


def coin_of(asset: object) -> str:
    return str(asset or "").strip().upper().split("/", 1)[0]


def level_covering(intervals: list[tuple], share: float) -> float:
    """Smallest load that is not exceeded for ``share`` of the time.

    Each interval is ``(start, end)`` or ``(start, end, weight)``. The weight is
    the share of a slot the position takes (0.25 for a probation trade, 1.0 at
    full size), so the answer is in slots. Time is measured from the first
    interval's start to the last one's end, so a young fleet is not diluted by
    days before it existed.
    """
    events: list[tuple[datetime, float]] = []
    for item in intervals:
        start, end = item[0], item[1]
        weight = float(item[2]) if len(item) > 2 else 1.0
        if end <= start or weight <= 0:
            continue
        events.append((start, weight))
        events.append((end, -weight))
    if not events:
        return 0.0
    events.sort(key=lambda e: (e[0], e[1]))
    seconds: dict[float, float] = {}
    level, last = 0.0, events[0][0]
    for moment, step in events:
        if moment > last:
            key = round(level, 6)
            seconds[key] = seconds.get(key, 0.0) + (moment - last).total_seconds()
        level += step
        last = moment
    total = sum(seconds.values())
    if total <= 0:
        return 0.0
    if share >= 1.0:
        return max(seconds)
    running = 0.0
    for lvl in sorted(seconds):
        running += seconds[lvl]
        if running / total >= share:
            return lvl
    return max(seconds)


def slot_plan(
    equity_usd: float,
    intervals: list[tuple],
    *,
    coverage_pct: float,
    leverage: float,
    smallest_size_pct: float,
) -> dict:
    """Slots the account runs with: what history asks for, capped by capital.

    A slot is one full-size position. Live strategies trade at different sizes
    at the same time (probation at 25% or 50%, graduated at 100%), and each
    takes that share of a slot. Capital sets the ceiling: a slot must be large
    enough that an order at the SMALLEST size in use still clears the exchange
    minimum. History sets the ask: the load, in slots, that covers
    ``coverage_pct`` of the time, plus one spare. Every figure comes from the
    install's own balance, so a $100 account and a $10,000 account each get
    their own answer.
    """
    order_per_slot_dollar = max(leverage, 0.0) * max(smallest_size_pct, 0.0) / 100.0
    capital_cap = int(equity_usd * order_per_slot_dollar // MIN_ORDER_USD) if equity_usd > 0 else 0
    wanted = math.ceil(level_covering(intervals, min(max(coverage_pct, 0.0), 100.0) / 100.0) - 1e-9) + 1
    slots = max(min(wanted, capital_cap), 1)
    return {
        "slots": slots,
        "slots_wanted": wanted,
        "slots_capital_cap": capital_cap,
        "capital_bound": wanted > capital_cap,
        "slot_usd": round(equity_usd / slots, 4) if equity_usd > 0 else 0.0,
    }


def disaster_stop(rows: list[dict], net_units: float, gap_pct: float) -> float | None:
    """Just beyond the furthest strategy stop on the side the net position is on."""
    gap = max(gap_pct, 0.0) / 100.0
    if net_units > _EPS:
        stops = [r["stop_price"] for r in rows if r["side"] > 0 and r.get("stop_price")]
        return min(stops) * (1.0 - gap) if stops else None
    if net_units < -_EPS:
        stops = [r["stop_price"] for r in rows if r["side"] < 0 and r.get("stop_price")]
        return max(stops) * (1.0 + gap) if stops else None
    return None


def order_for(target_units: float, held_units: float, price: float) -> dict | None:
    """The one order that moves the exchange position to the sum of the rows.

    None while the difference is under the exchange minimum: it stays a
    residual and rides along with the next order on the coin.
    """
    delta = target_units - held_units
    notional = abs(delta) * price
    if notional + 1e-9 < MIN_ORDER_USD:
        return None
    return {
        "delta_units": delta,
        "price": price,
        "notional_usd": notional,
        "fee_usd": notional * TAKER_FEE_FRAC,
        "net_after": target_units,
    }


# --- settings and inputs --------------------------------------------------------


def settings() -> dict:
    raw = kv_get(SETTINGS_KV_KEY, {})
    stored = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    out["live_netting_shadow_enabled"] = _as_bool(stored.get("live_netting_shadow_enabled"), False)
    for key, default in DEFAULTS.items():
        if key == "live_netting_shadow_enabled":
            continue
        try:
            value = float(stored.get(key, default))
        except (TypeError, ValueError):
            value = float(default)
        out[key] = value if math.isfinite(value) and value > 0 else float(default)
    return out


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y"}
    return default


def enabled() -> bool:
    try:
        return bool(settings()["live_netting_shadow_enabled"])
    except Exception:  # noqa: BLE001 - an unreadable setting means off
        return False


def set_enabled(value: bool) -> dict:
    raw = kv_get(SETTINGS_KV_KEY, {})
    stored = dict(raw) if isinstance(raw, dict) else {}
    stored["live_netting_shadow_enabled"] = bool(value)
    kv_set(SETTINGS_KV_KEY, stored)
    return status()


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _equity(cfg: dict) -> float | None:
    """The install's own live balance, or None when there is none to read."""
    try:
        daemon_state = kv_get("daemon_state", {})
        value = float((daemon_state or {}).get("account_equity") or 0.0)
        if math.isfinite(value) and value > 0:
            return value
    except Exception:  # noqa: BLE001 - fall through to the optional stand-in
        pass
    stand_in = float(cfg["live_netting_shadow_equity_usd"])
    return stand_in if stand_in > 0 else None


def cohort_ids(conn) -> set[str]:
    """Strategies the engine would trade: live now, or passing every probation check."""
    marks = ",".join("?" * len(LIVE_STAGES))
    ids = {
        str(r[0])
        for r in conn.execute(
            f"SELECT id FROM strategies WHERE LOWER(COALESCE(stage, status, '')) IN ({marks})",
            LIVE_STAGES,
        ).fetchall()
    }
    ready = kv_get(READY_KV_KEY, None)
    for entry in (ready or {}).get("strategies", []) if isinstance(ready, dict) else []:
        if isinstance(entry, dict) and entry.get("eligible") and entry.get("strategy_id"):
            ids.add(str(entry["strategy_id"]))
    return ids


_PROBATION_REDUCED_STATES = ("active", "paused", "demoting")


def size_fractions(conn, cohort: set[str], start_size_pct: float) -> dict[str, float]:
    """Share of a slot each strategy trades at.

    Live at full size: 1.0. In live probation: its current probation size.
    Ready but not live yet: the size it would start probation at.
    """
    start = min(max(start_size_pct / 100.0, 0.0), 1.0)
    marks = ",".join("?" * len(LIVE_STAGES))
    live = {
        str(r[0])
        for r in conn.execute(
            f"SELECT id FROM strategies WHERE LOWER(COALESCE(stage, status, '')) IN ({marks})",
            LIVE_STAGES,
        ).fetchall()
    }
    out = {sid: (1.0 if sid in live else start) for sid in cohort}
    try:
        rows = conn.execute("SELECT strategy_id, state, size_pct FROM strategy_probation").fetchall()
    except Exception:  # noqa: BLE001 - no table yet: nobody is in probation
        rows = []
    for r in rows:
        sid = str(r[0])
        if sid not in live or sid not in out or str(r[1] or "") not in _PROBATION_REDUCED_STATES:
            continue
        try:
            out[sid] = min(max(float(r[2]) / 100.0, 0.0), 1.0)
        except (TypeError, ValueError):
            out[sid] = start
    return out


def _stop_of(signal_data: object) -> float | None:
    try:
        data = json.loads(signal_data) if isinstance(signal_data, str) else (signal_data or {})
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("stop_loss_price", "stop_loss"):
        try:
            value = float(data.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return None


def _paper_trades(conn, cohort: set[str], *, open_only: bool, since: datetime | None = None) -> list[dict]:
    if not cohort:
        return []
    marks = ",".join("?" * len(PAPER_TYPES))
    sql = (
        "SELECT id, COALESCE(strategy_id, strategy) AS sid, asset, direction, signal_data, "
        f"opened_at, closed_at, status FROM trades WHERE LOWER(COALESCE(execution_type, '')) IN ({marks})"
    )
    args: list[object] = list(PAPER_TYPES)
    if open_only:
        sql += " AND status = 'OPEN'"
    if since is not None:
        sql += " AND opened_at >= ?"
        args.append(since.isoformat())
    out = []
    for r in conn.execute(sql, args).fetchall():
        if str(r["sid"]) not in cohort:
            continue
        out.append({
            "trade_id": str(r["id"]),
            "strategy_id": str(r["sid"]),
            "coin": coin_of(r["asset"]),
            "side": -1 if str(r["direction"] or "").strip().lower() == "short" else 1,
            "stop_price": _stop_of(r["signal_data"]),
            "opened_at": r["opened_at"],
            "closed_at": r["closed_at"],
        })
    return out


# --- the tick -------------------------------------------------------------------


def _ensure_schema(conn) -> None:
    conn.executescript(SCHEMA_SQL)


def _load_state() -> dict:
    raw = kv_get(STATE_KV_KEY, {})
    state = dict(raw) if isinstance(raw, dict) else {}
    state.setdefault("held", {})
    state.setdefault("counters", {"entries_taken": 0, "entries_blocked": 0, "orders": 0, "fees_usd": 0.0})
    return state


def _refresh_slots(
    conn, cohort: set[str], sizes: dict[str, float], cfg: dict, state: dict, now: datetime
) -> None:
    if state.get("slots") and time.time() - float(state.get("slots_refreshed_ts") or 0.0) < SLOT_REFRESH_SECONDS:
        return
    history = _paper_trades(conn, cohort, open_only=False, since=now - timedelta(days=HISTORY_DAYS))
    intervals = []
    for t in history:
        start = _parse_ts(t["opened_at"])
        if start is None:
            continue
        intervals.append((start, _parse_ts(t["closed_at"]) or now, sizes.get(t["strategy_id"], 1.0)))
    equity = _equity(cfg) or 0.0
    plan = slot_plan(
        equity,
        intervals,
        coverage_pct=cfg["live_netting_slot_coverage_pct"],
        leverage=cfg["live_netting_leverage"],
        smallest_size_pct=100.0 * min(sizes.values(), default=1.0),
    )
    state.update(plan)
    state["equity_usd"] = round(equity, 2)
    state["cohort_size"] = len(cohort)
    state["slots_refreshed_ts"] = time.time()


def tick(prices: dict[str, float], *, now: datetime | None = None) -> dict | None:
    """One shadow pass. Returns a short summary, or None when the shadow is off.

    Reads paper trades and prices, writes only the shadow's own tables and state.
    """
    cfg = settings()
    if not cfg["live_netting_shadow_enabled"]:
        return None
    now = now or _now()
    if _equity(cfg) is None:
        # No live balance on this install: nothing to size from, so record nothing.
        state = _load_state()
        state["idle_reason"] = "no_live_balance"
        state["last_tick_at"] = now.isoformat()
        kv_set(STATE_KV_KEY, state)
        return {"opened": 0, "closed": 0, "blocked": 0, "orders": 0}
    marks = {coin_of(k): float(v) for k, v in (prices or {}).items() if v and float(v) > 0}
    state = _load_state()
    opened = closed = blocked = 0
    new_orders: list[dict] = []

    with get_db() as conn:
        _ensure_schema(conn)
        cohort = cohort_ids(conn)
        sizes = size_fractions(conn, cohort, cfg["live_netting_shadow_size_pct"])
        _refresh_slots(conn, cohort, sizes, cfg, state, now)
        slots = int(state.get("slots") or 1)
        slot_usd = float(state.get("slot_usd") or 0.0)
        full_order_usd = slot_usd * cfg["live_netting_leverage"]

        paper_open = {t["trade_id"]: t for t in _paper_trades(conn, cohort, open_only=True)}
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM netting_shadow_rows WHERE status IN ('open', 'blocked')"
        ).fetchall()]
        known = {r["trade_id"] for r in rows}
        touched: dict[str, set[str]] = {}

        # A strategy left its paper trade: close its row at the market.
        for row in rows:
            if row["trade_id"] in paper_open:
                continue
            if row["status"] == "blocked":
                conn.execute(
                    "UPDATE netting_shadow_rows SET status = 'blocked_closed', closed_at = ? WHERE trade_id = ?",
                    (now.isoformat(), row["trade_id"]),
                )
                row["status"] = "blocked_closed"
                continue
            price = marks.get(row["coin"])
            if price is None:
                continue  # no mark this tick: keep the row and try again
            pnl = row["side"] * row["units"] * (price - float(row["entry_price"] or price))
            conn.execute(
                "UPDATE netting_shadow_rows SET status = 'closed', closed_at = ?, exit_price = ?, pnl_usd = ? "
                "WHERE trade_id = ?",
                (now.isoformat(), price, pnl, row["trade_id"]),
            )
            row["status"] = "closed"
            touched.setdefault(row["coin"], set()).add(row["strategy_id"])
            closed += 1

        open_rows = [r for r in rows if r["status"] == "open"]

        # A strategy entered a paper trade: take a slot, or record the miss.
        for trade_id, trade in sorted(paper_open.items(), key=lambda kv: str(kv[1]["opened_at"] or "")):
            if trade_id in known:
                continue
            price = marks.get(trade["coin"])
            if price is None:
                continue
            size = sizes.get(trade["strategy_id"], 1.0)
            order_usd = full_order_usd * size
            in_use = sum(float(r.get("size_frac") or 1.0) for r in open_rows)
            if in_use + size > slots + 1e-9:
                status_, reason, units = "blocked", "slots_full", 0.0
                blocked += 1
            elif order_usd + 1e-9 < MIN_ORDER_USD:
                status_, reason, units = "blocked", "order_under_minimum", 0.0
                blocked += 1
            else:
                status_, reason, units = "open", None, order_usd / price
                opened += 1
            row = {
                "trade_id": trade_id, "strategy_id": trade["strategy_id"], "coin": trade["coin"],
                "side": trade["side"], "units": units, "size_frac": size, "entry_price": price,
                "stop_price": trade["stop_price"], "status": status_, "reason": reason,
            }
            conn.execute(
                "INSERT INTO netting_shadow_rows (trade_id, strategy_id, coin, side, units, size_frac, entry_price, "
                "stop_price, status, reason, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade_id, row["strategy_id"], row["coin"], row["side"], units, size, price,
                 row["stop_price"], status_, reason, now.isoformat()),
            )
            if status_ == "open":
                open_rows.append(row)
                touched.setdefault(row["coin"], set()).add(row["strategy_id"])

        # One order per coin for the difference between the rows and what is held.
        held: dict[str, float] = {k: float(v) for k, v in (state.get("held") or {}).items()}
        books: dict[str, dict] = {}
        for coin in sorted({r["coin"] for r in open_rows} | set(held)):
            coin_rows = [r for r in open_rows if r["coin"] == coin]
            target = sum(r["side"] * r["units"] for r in coin_rows)
            price = marks.get(coin)
            current = held.get(coin, 0.0)
            if price is not None:
                order = order_for(target, current, price)
                if order is not None:
                    conn.execute(
                        "INSERT INTO netting_shadow_orders (at, coin, delta_units, price, notional_usd, fee_usd, "
                        "net_after, strategies) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (now.isoformat(), coin, order["delta_units"], price, order["notional_usd"],
                         order["fee_usd"], target, ",".join(sorted(touched.get(coin, set())))),
                    )
                    new_orders.append({"coin": coin, **order})
                    current = target
            if abs(current) < _EPS and not coin_rows:
                held.pop(coin, None)
                continue
            held[coin] = current
            gross = sum(r["units"] for r in coin_rows)
            books[coin] = {
                "strategies": len(coin_rows),
                "longs": sum(1 for r in coin_rows if r["side"] > 0),
                "shorts": sum(1 for r in coin_rows if r["side"] < 0),
                "net_units": target,
                "gross_units": gross,
                "held_units": current,
                "residual_units": target - current,
                "mark": price,
                "disaster_stop": disaster_stop(coin_rows, target, cfg["live_netting_disaster_gap_pct"]),
            }

    counters = state["counters"]
    counters["entries_taken"] = int(counters.get("entries_taken", 0)) + opened
    counters["entries_blocked"] = int(counters.get("entries_blocked", 0)) + blocked
    counters["orders"] = int(counters.get("orders", 0)) + len(new_orders)
    counters["fees_usd"] = float(counters.get("fees_usd", 0.0)) + sum(o["fee_usd"] for o in new_orders)
    state.pop("idle_reason", None)
    state["held"] = held
    state["books"] = books
    state["slots_in_use"] = round(sum(float(r.get("size_frac") or 1.0) for r in open_rows), 4)
    state["positions_open"] = len(open_rows)
    state["last_tick_at"] = now.isoformat()
    state.setdefault("started_at", now.isoformat())
    kv_set(STATE_KV_KEY, state)
    return {"opened": opened, "closed": closed, "blocked": blocked, "orders": len(new_orders)}


# --- reading it back ------------------------------------------------------------


def status(limit: int = 50) -> dict:
    """What the shadow holds now, plus its recent rows and orders."""
    cfg = settings()
    state = _load_state()
    recent_rows: list[dict] = []
    recent_orders: list[dict] = []
    realised = 0.0
    if state.get("started_at"):
        with get_db() as conn:
            _ensure_schema(conn)
            recent_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM netting_shadow_rows ORDER BY COALESCE(closed_at, opened_at) DESC LIMIT ?",
                (int(limit),),
            ).fetchall()]
            recent_orders = [dict(r) for r in conn.execute(
                "SELECT * FROM netting_shadow_orders ORDER BY id DESC LIMIT ?", (int(limit),)
            ).fetchall()]
            got = conn.execute(
                "SELECT COALESCE(SUM(pnl_usd), 0) FROM netting_shadow_rows WHERE status = 'closed'"
            ).fetchone()
            realised = float(got[0] or 0.0)
    counters = state.get("counters") or {}
    return {
        "enabled": bool(cfg["live_netting_shadow_enabled"]),
        "sends_orders": False,
        "settings": {k: v for k, v in cfg.items() if k != "live_netting_shadow_enabled"},
        "idle_reason": state.get("idle_reason"),
        "started_at": state.get("started_at"),
        "last_tick_at": state.get("last_tick_at"),
        "equity_usd": state.get("equity_usd"),
        "cohort_size": state.get("cohort_size"),
        "slots": state.get("slots"),
        "slots_wanted": state.get("slots_wanted"),
        "slots_capital_cap": state.get("slots_capital_cap"),
        "capital_bound": state.get("capital_bound"),
        "slot_usd": state.get("slot_usd"),
        "slots_in_use": state.get("slots_in_use", 0),
        "positions_open": state.get("positions_open", 0),
        "counters": counters,
        "realised_pnl_usd": round(realised, 4),
        "net_after_fees_usd": round(realised - float(counters.get("fees_usd", 0.0)), 4),
        "books": state.get("books") or {},
        "recent_rows": recent_rows,
        "recent_orders": recent_orders,
    }


def reset() -> dict:
    """Forget everything the shadow recorded. Touches only the shadow's own data."""
    with get_db() as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM netting_shadow_rows")
        conn.execute("DELETE FROM netting_shadow_orders")
    kv_set(STATE_KV_KEY, {})
    return status()
