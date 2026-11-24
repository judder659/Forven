"""Stage 0 dry run for the multi-strategy live engine (read-only).

Replays recorded PAPER trades through the planned engine rules and reports what
would have happened had those strategies shared one live account:

  * how many strategies are in a trade at once (sets the slot count),
  * how many entries each slot-coverage level would have blocked,
  * how many entries today's one-strategy-per-coin rule blocks,
  * how often strategies share a coin, in the same or opposite direction,
  * how much smaller the net exchange position is than the gross.

It opens the database read-only, sends no orders and writes nothing but the
optional ``--out`` report. Standard library only, so it runs on any install:

    python scripts/netting_dry_run.py --days 60 --out netting_dry_run.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

PAPER_TYPES = ("paper", "paper_challenger", "simulation")
LIVE_STAGES = ("live_graduated", "deployed")
COVERAGE_LEVELS = (0.90, 0.95, 0.99, 1.0)
READY_KV_KEY = "forven:probation:ready"
MIN_ORDER_USD = 10.0
TODAY_POSITION_CAP = 10


def _default_db() -> Path:
    home = os.environ.get("FORVEN_HOME")
    return (Path(home) if home else Path.home() / ".forven") / "forven.db"


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _coin(asset: object) -> str:
    return str(asset or "").strip().upper().split("/", 1)[0]


def _ready_ids(conn: sqlite3.Connection) -> set[str]:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (READY_KV_KEY,)).fetchone()
    if not row or not row[0]:
        return set()
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return set()
    entries = payload.get("strategies") if isinstance(payload, dict) else None
    ids: set[str] = set()
    for entry in entries or []:
        if isinstance(entry, dict):
            sid = entry.get("strategy_id") or entry.get("id")
        else:
            sid = entry
        if sid:
            ids.add(str(sid))
    return ids


def _stage_ids(conn: sqlite3.Connection, stages: tuple[str, ...]) -> set[str]:
    marks = ",".join("?" * len(stages))
    rows = conn.execute(
        f"SELECT id FROM strategies WHERE LOWER(COALESCE(stage, status, '')) IN ({marks})", stages
    ).fetchall()
    return {str(r[0]) for r in rows}


def load_trades(conn: sqlite3.Connection, start: datetime, now: datetime) -> list[dict]:
    marks = ",".join("?" * len(PAPER_TYPES))
    rows = conn.execute(
        f"SELECT COALESCE(strategy_id, strategy), asset, direction, opened_at, closed_at, "
        f"COALESCE(net_pnl_pct, pnl_pct) FROM trades "
        f"WHERE LOWER(COALESCE(execution_type, '')) IN ({marks}) AND opened_at >= ?",
        (*PAPER_TYPES, start.isoformat()),
    ).fetchall()
    trades = []
    for sid, asset, direction, opened_at, closed_at, pnl in rows:
        opened = _parse_ts(opened_at)
        if not sid or opened is None or opened < start:
            continue
        closed = _parse_ts(closed_at) or now
        if closed <= opened:
            closed = opened + timedelta(seconds=1)
        trades.append({
            "strategy": str(sid),
            "coin": _coin(asset),
            "side": 1 if str(direction or "").strip().lower() != "short" else -1,
            "open": opened,
            "close": closed,
            "pnl": float(pnl) if pnl is not None else None,
        })
    trades.sort(key=lambda t: t["open"])
    return trades


def _time_at_level(trades: list[dict]) -> dict[int, float]:
    """Seconds spent with N trades open at once."""
    events = []
    for t in trades:
        events.append((t["open"], 1))
        events.append((t["close"], -1))
    events.sort(key=lambda e: (e[0], e[1]))
    seconds: dict[int, float] = defaultdict(float)
    level, last = 0, None
    for moment, step in events:
        if last is not None and moment > last and level > 0:
            seconds[level] += (moment - last).total_seconds()
        level += step
        last = moment
    return seconds


def _level_covering(seconds: dict[int, float], share: float, window_seconds: float) -> int:
    """Smallest N such that at most N trades are open for ``share`` of the window."""
    if not seconds:
        return 0
    if share >= 1.0:
        return max(seconds)
    busy = sum(seconds.values())
    total = max(window_seconds, busy)
    running = total - busy  # time with nothing open counts as level 0
    if running / total >= share:
        return 0
    for level in sorted(seconds):
        running += seconds[level]
        if running / total >= share:
            return level
    return max(seconds)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def simulate_slots(trades: list[dict], slots: int) -> dict:
    """First come, first served: an entry is blocked when every slot is taken."""
    active: list[datetime] = []
    taken, blocked = [], []
    for t in trades:
        active = [end for end in active if end > t["open"]]
        if len(active) < slots:
            active.append(t["close"])
            taken.append(t)
        else:
            blocked.append(t)
    return _split_report(taken, blocked)


def simulate_today(trades: list[dict]) -> dict:
    """Today's live rule: one strategy per coin, and a fixed position cap."""
    active: list[tuple[datetime, str]] = []
    taken, blocked = [], []
    for t in trades:
        active = [a for a in active if a[0] > t["open"]]
        coin_held = any(coin == t["coin"] for _, coin in active)
        if coin_held or len(active) >= TODAY_POSITION_CAP:
            blocked.append(t)
        else:
            active.append((t["close"], t["coin"]))
            taken.append(t)
    return _split_report(taken, blocked)


def _split_report(taken: list[dict], blocked: list[dict]) -> dict:
    total = len(taken) + len(blocked)
    return {
        "entries_taken": len(taken),
        "entries_blocked": len(blocked),
        "blocked_share_pct": round(100.0 * len(blocked) / total, 2) if total else 0.0,
        "avg_net_pnl_pct_taken": _mean([t["pnl"] for t in taken if t["pnl"] is not None]),
        "avg_net_pnl_pct_blocked": _mean([t["pnl"] for t in blocked if t["pnl"] is not None]),
    }


def coin_sharing(trades: list[dict]) -> dict:
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_coin[t["coin"]].append(t)

    held = shared = opposed = gross = net = 0.0
    joins_same = joins_opposite = 0
    busiest: list[tuple[int, str]] = []
    for coin, rows in by_coin.items():
        events = []
        for t in rows:
            events.append((t["open"], 0, t["side"], 1))
            events.append((t["close"], 1, t["side"], -1))
        # Closes (kind 1) sort after opens at the same instant on purpose: an
        # entry that lands exactly as another exits still counts as sharing.
        events.sort(key=lambda e: (e[0], e[1]))
        longs = shorts = peak = 0
        last = None
        for moment, kind, side, step in events:
            if last is not None and moment > last:
                span = (moment - last).total_seconds()
                count = longs + shorts
                if count:
                    held += span
                    gross += span * count
                    net += span * abs(longs - shorts)
                if count >= 2:
                    shared += span
                if longs and shorts:
                    opposed += span
            if kind == 0:
                same = longs if side > 0 else shorts
                other = shorts if side > 0 else longs
                if other:
                    joins_opposite += 1
                elif same:
                    joins_same += 1
            if side > 0:
                longs += step
            else:
                shorts += step
            peak = max(peak, longs + shorts)
            last = moment
        busiest.append((peak, coin))
    busiest.sort(reverse=True)
    entries = len(trades)
    return {
        "coins_traded": len(by_coin),
        "share_of_held_time_with_2plus_strategies_pct": round(100.0 * shared / held, 2) if held else 0.0,
        "share_of_held_time_long_and_short_together_pct": round(100.0 * opposed / held, 2) if held else 0.0,
        "entries_joining_same_direction_pct": round(100.0 * joins_same / entries, 2) if entries else 0.0,
        "entries_against_an_open_opposite_position_pct": round(100.0 * joins_opposite / entries, 2) if entries else 0.0,
        "net_position_as_share_of_gross_pct": round(100.0 * net / gross, 2) if gross else 100.0,
        "most_strategies_on_one_coin": [{"coin": c, "peak": p} for p, c in busiest[:8]],
    }


def cohort_report(trades: list[dict], members: set[str], days: int, equity: float, leverage: float) -> dict:
    rows = [t for t in trades if t["strategy"] in members]
    traders = {t["strategy"] for t in rows}
    window_seconds = days * 86400.0
    seconds = _time_at_level(rows)
    slot_table = []
    for share in COVERAGE_LEVELS:
        slots = max(_level_covering(seconds, share, window_seconds) + (0 if share >= 1.0 else 1), 1)
        slot_usd = equity / slots
        slot_table.append({
            "coverage_pct": round(share * 100),
            "slots": slots,
            "slot_usd": round(slot_usd, 2),
            "full_size_order_usd": round(slot_usd * leverage, 2),
            "probation_25pct_order_usd": round(slot_usd * leverage * 0.25, 2),
            "probation_order_clears_minimum": slot_usd * leverage * 0.25 >= MIN_ORDER_USD,
            **simulate_slots(rows, slots),
        })
    return {
        "strategies_in_cohort": len(members),
        "strategies_that_traded": len(traders),
        "entries": len(rows),
        "entries_per_strategy_per_30d": round(len(rows) / max(len(members), 1) * 30.0 / days, 2),
        "avg_net_pnl_pct_all_entries": _mean([t["pnl"] for t in rows if t["pnl"] is not None]),
        "peak_open_at_once": max(seconds) if seconds else 0,
        "today_one_per_coin_rule": simulate_today(rows),
        "slots_by_coverage": slot_table,
        "coin_sharing": coin_sharing(rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", type=Path, default=_default_db())
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--equity", type=float, default=1000.0, help="live account size in USD")
    parser.add_argument("--leverage", type=float, default=2.0)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db.as_posix()}?mode=ro", uri=True, timeout=30)
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=args.days)
    trades = load_trades(conn, start, now)
    live = _stage_ids(conn, LIVE_STAGES)
    ready = _ready_ids(conn)
    paper = _stage_ids(conn, ("paper",))
    conn.close()

    report = {
        "generated_at": now.isoformat(),
        "window_days": args.days,
        "assumed_equity_usd": args.equity,
        "assumed_leverage": args.leverage,
        "paper_entries_in_window": len(trades),
        "cohorts": {
            "ready_plus_live": cohort_report(trades, ready | live, args.days, args.equity, args.leverage),
            "all_paper": cohort_report(trades, paper | ready | live, args.days, args.equity, args.leverage),
        },
        "counts": {"live": len(live), "ready": len(ready), "paper": len(paper)},
    }
    text = json.dumps(report, indent=2)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
