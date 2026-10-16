"""Propr mirror scorecard (PROPR-SCORE-1).

Answers the question the /propr page could not: *who* is making or losing the
challenge account money, and is the mirror copying them faithfully?

Ground truth is the venue, not mirror state. Mirror state is display/retry
bookkeeping pruned after a week; the venue's fills carry realized PnL and fees.
Every mirrored entry's ``intentId`` is ``deterministic_ulid("propr-mirror:{trade_id}:entry")``,
so each venue entry order attributes exactly to its source Forven trade without
any stored mapping.

Read-only: nothing here places, cancels or sizes an order.

Venue history: the venue's list endpoints return at most the newest 100 rows
and the adapter exposes no offset, so every refresh merges the newest window
into a local ledger (kv ``forven:propr-mirror:venue-ledger``). History therefore
accumulates from the first refresh on; fills older than the venue's newest-100
window at that point are not recoverable through this path.

Attribution on a netted venue: Propr keeps one position per asset, so two
same-side mirrored trades can share a ``positionId``. A shared position's
realized PnL and fees are split across its attributed entries by filled
quantity. That is exact when the legs close together and approximate when they
do not; the per-trade rows flag ``shared_position`` so the page can say so.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from forven.db import get_db, kv_get, kv_set
from forven.propr_mirror import (
    MIN_VERDICT_TRADES,
    _r_multiple,
    _signal_stop,
    forward_records,
    track_record_verdict,
)
from forven.propr_mirror import _record_num as _num

log = logging.getLogger("forven.propr_scorecard")

LEDGER_KEY = "forven:propr-mirror:venue-ledger"
_VENUE_PAGE = 100
_CACHE_TTL_S = 60.0
_ACTIVE_STAGES = frozenset({"paper", "live_graduated"})

_cache: dict = {"at": 0.0, "value": None}


def _parse_when(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)



# --------------------------------------------------------------------------
# Venue ledger
# --------------------------------------------------------------------------

def _row_id(row: dict, *keys: str) -> str | None:
    for key in keys:
        value = row.get(key)
        if value:
            return str(value)
    return None


def refresh_venue_ledger(propr) -> dict:
    """Merge the venue's newest fills/orders into the local ledger and return it.

    A failed read leaves the stored ledger untouched (and is reported), so a
    venue outage degrades the scorecard to its last known history instead of
    blanking it."""
    ledger = kv_get(LEDGER_KEY, {}) or {}
    if not isinstance(ledger, dict):
        ledger = {}
    trades = dict(ledger.get("trades") or {})
    orders = dict(ledger.get("orders") or {})
    errors: dict[str, str] = {}
    try:
        for row in propr.list_trades(limit=_VENUE_PAGE) or []:
            tid = _row_id(row, "tradeId", "id")
            if tid:
                trades[tid] = row
    except Exception as exc:  # noqa: BLE001 — degrade to stored history
        errors["trades"] = str(exc)
    try:
        for row in propr.list_orders(limit=_VENUE_PAGE) or []:
            oid = _row_id(row, "orderId", "id")
            if oid:
                orders[oid] = row
    except Exception as exc:  # noqa: BLE001
        errors["orders"] = str(exc)
    ledger = {
        "trades": trades,
        "orders": orders,
        "refreshed_at": datetime.now(timezone.utc).isoformat(),
    }
    if len(errors) < 2:
        kv_set(LEDGER_KEY, ledger)
    return {**ledger, "errors": errors}


# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------

def _source_trades(since: datetime) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, strategy_id, strategy, asset, direction, entry_price, exit_price, "
            "status, execution_type, opened_at, closed_at, pnl_usd, signal_data "
            "FROM trades WHERE execution_type IN ('paper', 'live') AND opened_at >= ? "
            "ORDER BY opened_at",
            (since.isoformat(),),
        ).fetchall()
    return [dict(r) for r in rows]


def _strategy_rows(ids: set[str]) -> dict[str, dict]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT id, COALESCE(display_name, name) AS name, stage, status_reason, timeframe "
            f"FROM strategies WHERE id IN ({placeholders})",
            list(ids),
        ).fetchall()
    return {str(dict(r)["id"]): dict(r) for r in rows}


def _attribute(ledger: dict, sources: list[dict],
               open_position_ids: set[str] | None = None) -> tuple[list[dict], dict]:
    """Per mirrored source trade: venue fills, realized PnL, fees, R.

    ``open_position_ids`` is the venue's CURRENT book. When readable it is the
    truth for "still open": fill arithmetic alone misreads flipped or netted
    positions (a flip's closing quantity is not the entries' quantity), which
    showed long-flat July positions as open. Unreadable (None) falls back to
    comparing closed against entered quantity.

    Returns (mirrored_rows, unattributed_totals)."""
    from forven.exchange.propr import deterministic_ulid

    by_intent = {
        deterministic_ulid(f"propr-mirror:{s['id']}:entry"): s for s in sources
    }
    orders = ledger.get("orders") or {}
    fills = list((ledger.get("trades") or {}).values())

    fills_by_position: dict[str, list[dict]] = defaultdict(list)
    for fill in fills:
        fills_by_position[str(fill.get("positionId") or "")].append(fill)

    # Entry orders per position (non-reduce-only, with fills).
    entries_by_position: dict[str, list[tuple[dict, float, dict | None]]] = defaultdict(list)
    for order in orders.values():
        if order.get("reduceOnly"):
            continue
        qty = _num(order.get("cumulativeQuantity")) or 0.0
        if qty <= 0:
            continue
        pid = str(order.get("positionId") or "")
        entries_by_position[pid].append((order, qty, by_intent.get(str(order.get("intentId") or ""))))

    mirrored: list[dict] = []
    unattributed = {"realized_usd": 0.0, "fees_usd": 0.0, "positions": 0}
    positions = set(fills_by_position) | set(entries_by_position)
    for pid in positions:
        pfills = fills_by_position.get(pid, [])
        realized = sum(_num(f.get("realizedPnl")) or 0.0 for f in pfills)
        fees = sum(_num(f.get("fee")) or 0.0 for f in pfills)
        closes = [f for f in pfills if str(f.get("type") or "") in ("close", "reduce", "flip")]
        close_qty = sum(_num(f.get("quantity")) or 0.0 for f in closes)
        exit_avg = (
            sum((_num(f.get("price")) or 0.0) * (_num(f.get("quantity")) or 0.0) for f in closes)
            / close_qty if close_qty > 0 else None
        )
        closed_at = max((str(f.get("executedAt") or "") for f in closes), default="") or None
        entries = entries_by_position.get(pid, [])
        total_qty = sum(q for _o, q, _s in entries)
        if open_position_ids is not None:
            still_open = pid in open_position_ids
        else:
            still_open = close_qty + 1e-12 < total_qty
        attributed_qty = 0.0
        for order, qty, source in entries:
            if source is None:
                continue
            attributed_qty += qty
            share = qty / total_qty if total_qty > 0 else 1.0
            fill_entry = _num(order.get("averageFillPrice"))
            stop = _signal_stop(source.get("signal_data"))
            src_entry = _num(source.get("entry_price"))
            direction = str(source.get("direction") or "")
            sign = 1.0 if direction.lower().startswith("l") else -1.0
            pnl = (realized - fees) * share
            risk_usd = abs(fill_entry - stop) * qty if (fill_entry and stop) else None
            source_closed = str(source.get("status") or "").upper() != "OPEN"
            mirrored.append({
                "trade_id": str(source["id"]),
                "strategy_id": str(source.get("strategy_id") or source.get("strategy") or ""),
                "asset": source.get("asset"),
                "direction": direction.lower(),
                "execution_type": source.get("execution_type"),
                "opened_at": order.get("filledAt") or order.get("createdAt"),
                "closed_at": None if still_open else closed_at,
                "venue_open": still_open,
                "quantity": qty,
                "entry_fill": fill_entry,
                "exit_fill": exit_avg,
                "source_entry": src_entry,
                # Positive = the mirror got a WORSE price than the source.
                "entry_slippage_bps": (
                    round(-sign * (src_entry - fill_entry) / src_entry * 1e4, 2)
                    if (src_entry and fill_entry) else None
                ),
                "pnl_usd": round(pnl, 4),
                "fees_usd": round(fees * share, 4),
                "risk_usd": round(risk_usd, 4) if risk_usd else None,
                "r": round(pnl / risk_usd, 3) if (risk_usd and not still_open) else None,
                "source_r": (
                    round(_r_multiple(direction, src_entry, _num(source.get("exit_price")), stop), 3)
                    if source_closed and _r_multiple(direction, src_entry, _num(source.get("exit_price")), stop) is not None
                    else None
                ),
                "shared_position": len(entries) > 1,
            })
        if total_qty > 0 and attributed_qty + 1e-12 < total_qty:
            share = 1.0 - attributed_qty / total_qty
            unattributed["realized_usd"] += realized * share
            unattributed["fees_usd"] += fees * share
            unattributed["positions"] += 1
        elif total_qty <= 0 and pfills:
            unattributed["realized_usd"] += realized
            unattributed["fees_usd"] += fees
            unattributed["positions"] += 1
    mirrored.sort(key=lambda r: str(r.get("opened_at") or ""))
    unattributed = {k: round(v, 4) if isinstance(v, float) else v for k, v in unattributed.items()}
    return mirrored, unattributed


# --------------------------------------------------------------------------
# Scorecard
# --------------------------------------------------------------------------

def _health(stage: str | None, forward_n: int, forward_mean_r: float | None) -> dict:
    if stage is None:
        return {"level": "bad", "label": "Strategy missing",
                "detail": "No longer in the strategy table; it can never trade again."}
    if stage == "gauntlet":
        return {"level": "info", "label": "Not trading yet",
                "detail": "Still in the gauntlet; it mirrors once it reaches paper or live."}
    if stage not in _ACTIVE_STAGES:
        return {"level": "bad", "label": f"Inactive ({stage})",
                "detail": "Not in paper or live, so it emits no trades — but it still takes "
                          "an equal share of the account and shrinks every active member's size."}
    if forward_n >= MIN_VERDICT_TRADES and forward_mean_r is not None and forward_mean_r < 0:
        return {"level": "warn", "label": "Losing forward",
                "detail": f"{forward_n} forward trades averaging {forward_mean_r:+.2f}R."}
    if forward_n < MIN_VERDICT_TRADES:
        return {"level": "info", "label": "Too few trades",
                "detail": f"{forward_n} forward trade(s); under {MIN_VERDICT_TRADES} is noise, not a verdict."}
    return {"level": "ok", "label": "Earning",
            "detail": f"{forward_n} forward trades averaging {forward_mean_r:+.2f}R."}


def build_scorecard(propr, *, roster: dict[str, str], state: dict,
                    starting_balance: float | None, equity: float | None,
                    mode: str = "manual") -> dict:
    ledger = refresh_venue_ledger(propr)
    open_position_ids: set[str] | None
    try:
        open_position_ids = {
            str(p.get("positionId") or p.get("id") or "")
            for p in (propr.raw_positions() or [])
        }
    except Exception:  # noqa: BLE001 — fall back to fill arithmetic
        open_position_ids = None
    order_times = [
        _parse_when(o.get("createdAt")) for o in (ledger.get("orders") or {}).values()
    ]
    order_times = [t for t in order_times if t]
    join_times = [t for t in (_parse_when(v) for v in roster.values()) if t]
    horizon = min(order_times + join_times, default=datetime.now(timezone.utc) - timedelta(days=90))
    sources = _source_trades(horizon - timedelta(days=1))
    mirrored, unattributed = _attribute(ledger, sources, open_position_ids)
    mirrored_ids = {m["trade_id"] for m in mirrored}

    by_strategy: dict[str, list[dict]] = defaultdict(list)
    for m in mirrored:
        by_strategy[m["strategy_id"]].append(m)

    strategy_ids = set(roster) | set(by_strategy)
    meta = _strategy_rows(strategy_ids)

    rows = []
    for sid in sorted(strategy_ids):
        joined = _parse_when(roster.get(sid))
        mine = by_strategy.get(sid, [])
        closed = [m for m in mine if not m.get("venue_open")]
        rs = [m["r"] for m in closed if m.get("r") is not None]
        slips = [m["entry_slippage_bps"] for m in mine if m.get("entry_slippage_bps") is not None]

        # The strategy's own forward record since it joined (mirrored or not):
        # the mirror can only ever be as good as this.
        fwd = [
            s for s in sources
            if str(s.get("strategy_id") or s.get("strategy") or "") == sid
            and str(s.get("status") or "").upper() == "CLOSED"
            and (joined is None or (_parse_when(s.get("opened_at")) or horizon) >= joined)
        ]
        fwd_r = [
            r for r in (
                _r_multiple(str(s.get("direction") or ""), _num(s.get("entry_price")),
                            _num(s.get("exit_price")), _signal_stop(s.get("signal_data")))
                for s in fwd
            ) if r is not None
        ]
        missed = [s for s in fwd if str(s["id"]) not in mirrored_ids]
        skip_reasons: dict[str, int] = defaultdict(int)
        for s in missed:
            entry = state.get(str(s["id"])) if isinstance(state, dict) else None
            reason = (entry or {}).get("reason") if isinstance(entry, dict) else None
            skip_reasons[str(reason or "no record (older than the mirror's 7-day log)")] += 1

        fwd_mean = sum(fwd_r) / len(fwd_r) if fwd_r else None
        info = meta.get(sid)
        stage = info.get("stage") if info else None
        rows.append({
            "strategy_id": sid,
            "name": info.get("name") if info else None,
            "stage": stage,
            "timeframe": info.get("timeframe") if info else None,
            "on_roster": sid in roster,
            "joined_at": roster.get(sid),
            "health": _health(stage, len(fwd_r), fwd_mean),
            "mirror": {
                "trades": len(mine),
                "closed": len(closed),
                "wins": sum(1 for m in closed if (m.get("pnl_usd") or 0) > 0),
                "pnl_usd": round(sum(m.get("pnl_usd") or 0.0 for m in closed), 2),
                "fees_usd": round(sum(m.get("fees_usd") or 0.0 for m in mine), 2),
                "sum_r": round(sum(rs), 2) if rs else None,
                "mean_r": round(sum(rs) / len(rs), 3) if rs else None,
                "avg_entry_slippage_bps": round(sum(slips) / len(slips), 1) if slips else None,
            },
            "forward": {
                "trades": len(fwd),
                "with_r": len(fwd_r),
                "wins": sum(1 for r in fwd_r if r > 0),
                "sum_r": round(sum(fwd_r), 2) if fwd_r else None,
                "mean_r": round(fwd_mean, 3) if fwd_mean is not None else None,
                "missed": len(missed),
                "missed_reasons": dict(skip_reasons),
            },
        })

    realized_total = sum(m.get("pnl_usd") or 0.0 for m in mirrored if not m.get("venue_open"))
    trading_net = realized_total + unattributed["realized_usd"] - unattributed["fees_usd"]
    account_change = (
        round(float(equity) - float(starting_balance), 2)
        if equity is not None and starting_balance else None
    )
    all_r = [m["r"] for m in mirrored if m.get("r") is not None]
    all_src_r = [m["source_r"] for m in mirrored if m.get("r") is not None and m.get("source_r") is not None]
    inactive = [r["strategy_id"] for r in rows if r["on_roster"] and r["health"]["level"] == "bad"]
    records = forward_records()
    track = sorted(
        (
            {"strategy_id": sid, **rec, "verdict": track_record_verdict(rec),
             "on_roster": sid in roster}
            for sid, rec in records.items()
        ),
        key=lambda r: ({"qualified": 0, "probation": 1, "benched": 2}[r["verdict"]],
                       -(r["mean_r"] if r["mean_r"] is not None else -9), r["strategy_id"]),
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ledger_refreshed_at": ledger.get("refreshed_at"),
        "ledger_errors": ledger.get("errors") or {},
        "strategies": rows,
        "trades": mirrored[-200:],
        "account": {
            "starting_balance": starting_balance,
            "equity": equity,
            "change_usd": account_change,
            "trading_net_usd": round(trading_net, 2),
            # Funding, venue adjustments and anything else that moved the balance
            # without being a fill — derived, so labelled as such on the page.
            "other_usd": (
                round(account_change - trading_net, 2) if account_change is not None else None
            ),
            "unattributed": unattributed,
        },
        "fidelity": {
            "trades": len(all_r),
            "mirror_sum_r": round(sum(all_r), 2) if all_r else None,
            "source_sum_r": round(sum(all_src_r), 2) if all_src_r else None,
        },
        "inactive_roster": inactive,
        "track_record": {
            "mode": mode,
            "min_trades": MIN_VERDICT_TRADES,
            "strategies": track,
        },
    }


def invalidate_scorecard() -> None:
    _cache.update(at=0.0, value=None)


def get_scorecard(force: bool = False) -> dict:
    """Cached scorecard (one venue round-trip per minute at most)."""
    now = time.monotonic()
    if not force and _cache["value"] is not None and now - _cache["at"] < _CACHE_TTL_S:
        return _cache["value"]
    from forven import propr_mirror
    from forven.exchange import propr

    halt = propr_mirror.get_halt_state() or {}
    value = build_scorecard(
        propr,
        roster=propr_mirror.mirror_roster(),
        state=propr_mirror.get_state(),
        starting_balance=_num(halt.get("starting_balance")),
        equity=_num(halt.get("equity")),
        mode=propr_mirror.roster_mode(),
    )
    _cache.update(at=now, value=value)
    return value
