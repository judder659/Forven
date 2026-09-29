"""Decision journal for the trading desk: what each live or paper strategy did, and why.

One timeline per mode, built from the local database only:

* fills from ``trades`` (opened, closed, failed) with slippage against the
  signal price, the stop that protected the entry, and the recorded close reason;
* refused signals from ``scanner_signal_results``. The scanner re-evaluates every
  few minutes, so one refused signal repeats dozens of times. Consecutive refusals
  of the same strategy, signal and reason (numbers ignored) within
  ``EPISODE_GAP`` collapse into one episode with a count and a first/last time.
  Refused exits carry ``positioned``: whether a position was open at the time;
* regime-gate flags from ``regime_gate_events``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from forven.api_domains.live_fleet import (
    NOT_BLOCKS,
    _check_mode,
    _parse_ts,
    _reason_key,
    fleet_strategy_rows,
    held_at,
    trade_intervals,
)
from forven.db import get_db
from forven.trade_accounting import net_pnl_sql

EPISODE_GAP = timedelta(hours=6)
MAX_DAYS = 90
MAX_EVENTS = 1000
_SCANNER_TS_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"


def _signal_data(blob: object) -> dict[str, Any]:
    try:
        data = json.loads(blob) if isinstance(blob, str) else blob
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _num(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _trade_events(conn: Any, mode: str, since: datetime, strategy_id: str | None) -> list[dict[str, Any]]:
    pnl = net_pnl_sql()
    params: list[Any] = [mode, since.isoformat(), since.isoformat()]
    scope = ""
    if strategy_id:
        scope = " AND COALESCE(strategy_id, strategy) = ?"
        params.append(strategy_id)
    rows = conn.execute(
        f"""
        SELECT id, COALESCE(strategy_id, strategy) AS sid, asset, direction, status, size, leverage,
          entry_price, exit_price, signal_entry_price, signal_exit_price,
          entry_slippage_bps, exit_slippage_bps, opened_at, closed_at, failure_reason, book,
          signal_data,
          CASE WHEN UPPER(COALESCE(status, '')) = 'CLOSED' THEN ({pnl}) END AS net_pnl_usd
        FROM trades
        WHERE LOWER(COALESCE(execution_type, '')) = ?
          AND (datetime(opened_at) >= datetime(?) OR datetime(closed_at) >= datetime(?)){scope}
        """,
        params,
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        data = _signal_data(row["signal_data"])
        status = str(row["status"] or "").upper()
        base = {
            "strategy_id": row["sid"],
            "trade_id": row["id"],
            "asset": row["asset"],
            "direction": row["direction"],
            "size": _num(row["size"]),
            "leverage": _num(row["leverage"]),
            "book": row["book"],
        }
        stop = _num(data.get("stop_loss_price") or data.get("stop_loss"))
        exchange_stop = _num(data.get("exchange_stop_price"))
        if status == "FAILED":
            events.append({
                **base,
                "kind": "failed",
                "at": row["opened_at"],
                "price": _num(row["entry_price"]),
                "failure_reason": row["failure_reason"],
            })
            continue
        events.append({
            **base,
            "kind": "opened",
            "at": row["opened_at"],
            "price": _num(row["entry_price"]),
            "signal_price": _num(row["signal_entry_price"]),
            "slippage_bps": _num(row["entry_slippage_bps"]),
            "stop_price": exchange_stop if exchange_stop is not None else stop,
            "take_profit_price": _num(data.get("take_profit_price") or data.get("take_profit")),
            "risk_usd": _num(data.get("sizing_loss_at_stop_usd")),
            "source": data.get("source"),
        })
        if status == "CLOSED" and row["closed_at"]:
            net = _num(row["net_pnl_usd"])
            events.append({
                **base,
                "kind": "closed",
                "at": row["closed_at"],
                "price": _num(row["exit_price"]),
                "signal_price": _num(row["signal_exit_price"]),
                "slippage_bps": _num(row["exit_slippage_bps"]),
                "stop_price": exchange_stop if exchange_stop is not None else stop,
                "net_pnl_usd": round(net, 4) if net is not None else None,
                "close_reason": data.get("close_reason"),
                "exit_recovered_from": data.get("exit_recovered_from"),
            })
    return events


def _refusal_episodes(
    conn: Any,
    mode: str,
    since: datetime,
    strategy_ids: list[str],
) -> list[dict[str, Any]]:
    if not strategy_ids:
        return []
    ids = ",".join("?" for _ in strategy_ids)
    nb = ",".join("?" for _ in NOT_BLOCKS)
    rows = conn.execute(
        f"""
        SELECT ts, strategy_id, signal_type, price, block_reason
        FROM scanner_signal_results
        WHERE strategy_id IN ({ids}) AND ts >= ?
          AND matched = 1 AND executed = 0 AND signal_type IN ('entry', 'exit')
          AND COALESCE(block_reason, '') NOT IN ({nb})
        ORDER BY strategy_id, signal_type, ts
        """,
        (*strategy_ids, since.strftime(_SCANNER_TS_FORMAT), *NOT_BLOCKS),
    ).fetchall()
    episodes: list[dict[str, Any]] = []
    # One open episode per (strategy, signal, reason): a stray different refusal
    # in between does not split a run of the same one.
    open_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    intervals_by_sid: dict[str, list[tuple[datetime, datetime | None]]] = {}
    for row in rows:
        ts = _parse_ts(row["ts"])
        if ts is None:
            continue
        reason = str(row["block_reason"])
        key = (str(row["strategy_id"]), str(row["signal_type"]), _reason_key(reason))
        current = open_by_key.get(key)
        if current is not None and ts - current["_last"] <= EPISODE_GAP:
            current["count"] += 1
            current["at"] = row["ts"]
            current["_last"] = ts
            current["last_price"] = _num(row["price"])
            current["reason"] = reason
            if current["_positioned_known"] and not current["positioned"]:
                current["positioned"] = held_at(current["_intervals"], ts)
            continue
        sid = str(row["strategy_id"])
        intervals = intervals_by_sid.get(sid)
        if intervals is None:
            intervals = trade_intervals(conn, sid, mode)
            intervals_by_sid[sid] = intervals
        is_exit = row["signal_type"] == "exit"
        current = {
            "_key": key,
            "_last": ts,
            "_intervals": intervals,
            "_positioned_known": is_exit,
            "kind": "exit_refused" if is_exit else "entry_refused",
            "strategy_id": sid,
            "first_at": row["ts"],
            "at": row["ts"],
            "count": 1,
            "reason": reason,
            "price": _num(row["price"]),
            "last_price": _num(row["price"]),
            "positioned": held_at(intervals, ts) if is_exit else None,
        }
        open_by_key[key] = current
        episodes.append(current)
    for episode in episodes:
        for private in ("_key", "_last", "_intervals", "_positioned_known"):
            episode.pop(private, None)
    return episodes


def _regime_events(conn: Any, mode: str, since: datetime, strategy_id: str | None) -> list[dict[str, Any]]:
    params: list[Any] = [mode, since.isoformat()]
    scope = ""
    if strategy_id:
        scope = " AND strategy_id = ?"
        params.append(strategy_id)
    try:
        rows = conn.execute(
            f"""
            SELECT ts, strategy_id, asset, direction, regime, confidence, mode, decision, ref_price, mtm_pct
            FROM regime_gate_events
            WHERE LOWER(COALESCE(execution_type, '')) = ? AND datetime(ts) >= datetime(?){scope}
            """,
            params,
        ).fetchall()
    except Exception:  # noqa: BLE001 — an older DB without the ledger still gets a journal
        return []
    return [
        {
            "kind": "regime_flag",
            "at": row["ts"],
            "strategy_id": row["strategy_id"],
            "asset": row["asset"],
            "direction": row["direction"],
            "regime": row["regime"],
            "confidence": _num(row["confidence"]),
            "gate_mode": row["mode"],
            "decision": row["decision"],
            "price": _num(row["ref_price"]),
            "mtm_pct": _num(row["mtm_pct"]),
        }
        for row in rows
    ]


def build_journal(
    mode: str = "live",
    *,
    strategy_id: str | None = None,
    days: int = 30,
    limit: int = 400,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Newest-first decision timeline for ``mode`` (optionally one strategy)."""
    mode = _check_mode(mode)
    now = now or datetime.now(timezone.utc)
    days = max(1, min(int(days or 30), MAX_DAYS))
    limit = max(1, min(int(limit or 400), MAX_EVENTS))
    since = now - timedelta(days=days)
    sid = str(strategy_id or "").strip() or None
    with get_db() as conn:
        if sid:
            scope_ids = [sid]
        else:
            scope_ids = [str(row["id"]) for row in fleet_strategy_rows(conn, mode)]
        events = [
            *_trade_events(conn, mode, since, sid),
            *_refusal_episodes(conn, mode, since, scope_ids),
            *_regime_events(conn, mode, since, sid),
        ]
    events.sort(key=lambda event: _parse_ts(event.get("at")) or since, reverse=True)
    counts: dict[str, int] = {}
    for event in events:
        counts[event["kind"]] = counts.get(event["kind"], 0) + 1
    return {
        "mode": mode,
        "strategy_id": sid,
        "generated_at": now.isoformat(),
        "window_days": days,
        "episode_gap_hours": EPISODE_GAP.total_seconds() / 3600,
        "counts": counts,
        "truncated": len(events) > limit,
        "events": events[:limit],
    }
