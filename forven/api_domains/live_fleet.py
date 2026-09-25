"""Live fleet scorecard for the dashboard: what each live strategy is doing with real money.

Built only from the local database (no exchange calls), so it stays cheap on the
single-worker API. Live means ``execution_type = 'live'`` trade rows and
strategies at the ``live_graduated`` stage. The global ``execution_mode`` setting
is a label only; a strategy's stage decides whether it trades real money.

Entry blocks come from ``scanner_signal_results``. The scanner writes an
``evaluation_only`` placeholder for every matched signal before execution, and
``no_actionable_position_or_order`` when an entry signal simply continues an
open position. Neither is a block, so both are excluded along with pre-outcome
rows that carry no reason.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from forven.db import get_db
from forven.trade_accounting import net_pnl_sql

LIVE_STAGE = "live_graduated"
BLOCK_WINDOW_DAYS = 30
# A blocked entry keeps a strategy in the BLOCKED state until a later live entry
# fills, or until the block is this old.
BLOCKED_STATE_MAX_AGE = timedelta(days=7)
DEFAULT_SCAN_INTERVAL_SECONDS = 300
# Healthy evaluation gaps reach ~15 min on a 5-minute cadence (observed 2026-09-25),
# so "not scanned" needs a wide margin to avoid false alarms on open positions.
STALE_AFTER_SCAN_INTERVALS = 6
MIN_STALE_AFTER_SECONDS = 1800
RECENT_FILLS_LIMIT = 10
_NOT_BLOCKS = ("", "evaluation_only", "no_actionable_position_or_order", "no_signal")
_SCANNER_TS_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _scan_interval_seconds(conn: Any) -> int:
    row = conn.execute(
        "SELECT schedule_type, schedule_expr FROM scheduler_jobs WHERE id = 'forven-scanner-signal'"
    ).fetchone()
    if row and str(row["schedule_type"] or "") == "interval":
        try:
            seconds = int(float(row["schedule_expr"]) / 1000)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            return seconds
    return DEFAULT_SCAN_INTERVAL_SECONDS


def _live_bots_armed(conn: Any) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM bot_configs WHERE LOWER(COALESCE(execution_mode, '')) = 'live'"
    ).fetchone()
    return int(row[0] or 0)


def count_live_armed() -> dict[str, int]:
    """Strategies at the live stage and bots armed for live execution."""
    with get_db() as conn:
        strategies = conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE LOWER(COALESCE(stage, status, '')) = ?",
            (LIVE_STAGE,),
        ).fetchone()[0]
        bots = _live_bots_armed(conn)
    return {"strategies": int(strategies or 0), "bots": bots}


def _trade_stats(conn: Any, strategy_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not strategy_ids:
        return {}
    pnl = net_pnl_sql()
    closed = "UPPER(COALESCE(status, '')) = 'CLOSED'"
    placeholders = ",".join("?" for _ in strategy_ids)
    rows = conn.execute(
        f"""
        SELECT COALESCE(strategy_id, strategy) AS sid,
          SUM(CASE WHEN {closed} THEN 1 ELSE 0 END) AS closed,
          SUM(CASE WHEN {closed} AND ({pnl}) > 0 THEN 1 ELSE 0 END) AS wins,
          SUM(CASE WHEN {closed} AND ({pnl}) < 0 THEN 1 ELSE 0 END) AS losses,
          SUM(CASE WHEN UPPER(COALESCE(status, '')) = 'FAILED' THEN 1 ELSE 0 END) AS failed,
          SUM(CASE WHEN {closed} THEN COALESCE(({pnl}), 0) ELSE 0 END) AS net_pnl_usd,
          MAX(COALESCE(closed_at, opened_at)) AS last_trade_at,
          MAX(CASE WHEN UPPER(COALESCE(status, '')) != 'FAILED' THEN opened_at END) AS last_open_at,
          GROUP_CONCAT(CASE WHEN UPPER(COALESCE(status, '')) = 'OPEN' THEN id END) AS open_ids
        FROM trades
        WHERE LOWER(COALESCE(execution_type, '')) = 'live'
          AND COALESCE(strategy_id, strategy) IN ({placeholders})
        GROUP BY sid
        """,
        tuple(strategy_ids),
    ).fetchall()
    return {str(row["sid"]): dict(row) for row in rows}


def _reason_key(reason: str) -> str:
    return _NUMBER_RE.sub("#", reason)


def _blocked_entries(conn: Any, strategy_id: str, since: datetime) -> dict[str, Any]:
    placeholders = ",".join("?" for _ in _NOT_BLOCKS)
    rows = conn.execute(
        f"""
        SELECT ts, block_reason FROM scanner_signal_results
        WHERE strategy_id = ? AND ts >= ?
          AND signal_type = 'entry' AND matched = 1 AND executed = 0
          AND COALESCE(block_reason, '') NOT IN ({placeholders})
        ORDER BY ts DESC
        """,
        (strategy_id, since.strftime(_SCANNER_TS_FORMAT), *_NOT_BLOCKS),
    ).fetchall()
    summary: dict[str, Any] = {
        "count": len(rows),
        "last_at": None,
        "last_reason": None,
        "top_reason": None,
        "top_count": 0,
    }
    if not rows:
        return summary
    summary["last_at"] = rows[0]["ts"]
    summary["last_reason"] = rows[0]["block_reason"]
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        reason = str(row["block_reason"])
        group = groups.setdefault(_reason_key(reason), {"count": 0, "reason": reason})
        group["count"] += 1
    top = max(groups.values(), key=lambda group: group["count"])
    summary["top_reason"] = top["reason"]
    summary["top_count"] = top["count"]
    return summary


def _last_scan(conn: Any, strategy_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT ts, signal_type, matched, executed, block_reason FROM scanner_signal_results
        WHERE strategy_id = ? ORDER BY ts DESC LIMIT 1
        """,
        (strategy_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "at": row["ts"],
        "signal_type": row["signal_type"],
        "matched": bool(row["matched"]),
        "executed": bool(row["executed"]),
        "reason": row["block_reason"],
    }


def _strategy_state(
    *,
    open_ids: list[str],
    last_scan_at: datetime | None,
    last_blocked_at: datetime | None,
    last_open_at: datetime | None,
    now: datetime,
    stale_after: timedelta,
) -> str:
    # Staleness wins: an open position the scanner no longer evaluates cannot exit.
    if last_scan_at is None or now - last_scan_at > stale_after:
        return "stale"
    if open_ids:
        return "in_position"
    if (
        last_blocked_at is not None
        and now - last_blocked_at <= BLOCKED_STATE_MAX_AGE
        and (last_open_at is None or last_blocked_at > last_open_at)
    ):
        return "blocked"
    return "watching"


def _realized(conn: Any, now: datetime) -> dict[str, dict[str, Any]]:
    pnl = net_pnl_sql()
    windows = {"7d": now - timedelta(days=7), "30d": now - timedelta(days=30), "all": None}
    out: dict[str, dict[str, Any]] = {}
    for label, since in windows.items():
        where = (
            "LOWER(COALESCE(execution_type, '')) = 'live' "
            "AND UPPER(COALESCE(status, '')) = 'CLOSED' AND closed_at IS NOT NULL"
        )
        params: tuple[Any, ...] = ()
        if since is not None:
            where += " AND datetime(closed_at) >= datetime(?)"
            params = (since.isoformat(),)
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS closed,
              SUM(CASE WHEN ({pnl}) > 0 THEN 1 ELSE 0 END) AS wins,
              SUM(CASE WHEN ({pnl}) < 0 THEN 1 ELSE 0 END) AS losses,
              SUM(CASE WHEN ({pnl}) > 0 THEN ({pnl}) ELSE 0 END) AS gross_profit,
              SUM(CASE WHEN ({pnl}) < 0 THEN ({pnl}) ELSE 0 END) AS gross_loss,
              SUM(COALESCE(({pnl}), 0)) AS net_pnl_usd
            FROM trades WHERE {where}
            """,
            params,
        ).fetchone()
        wins = int(row["wins"] or 0)
        losses = int(row["losses"] or 0)
        gross_loss = float(row["gross_loss"] or 0.0)
        out[label] = {
            "closed": int(row["closed"] or 0),
            "wins": wins,
            "losses": losses,
            "net_pnl_usd": round(float(row["net_pnl_usd"] or 0.0), 2),
            "win_rate": (wins / (wins + losses)) if wins + losses else None,
            "profit_factor": (
                float(row["gross_profit"] or 0.0) / abs(gross_loss) if gross_loss < 0 else None
            ),
        }
    return out


def _close_reason(signal_data: object) -> str | None:
    try:
        data = json.loads(signal_data) if isinstance(signal_data, str) else signal_data
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    reason = str(data.get("close_reason") or "").strip()
    return reason or None


def _recent_fills(conn: Any) -> list[dict[str, Any]]:
    pnl = net_pnl_sql()
    rows = conn.execute(
        f"""
        SELECT t.id, COALESCE(t.strategy_id, t.strategy) AS strategy_id,
          COALESCE(NULLIF(s.display_name, ''), s.name, t.strategy_name) AS strategy_name,
          t.asset, t.direction, t.status, t.opened_at, t.closed_at, t.failure_reason,
          t.signal_data,
          CASE WHEN UPPER(COALESCE(t.status, '')) = 'CLOSED' THEN ({pnl}) END AS net_pnl_usd
        FROM trades t
        LEFT JOIN strategies s ON s.id = COALESCE(t.strategy_id, t.strategy)
        WHERE LOWER(COALESCE(t.execution_type, '')) = 'live'
        ORDER BY datetime(COALESCE(t.closed_at, t.opened_at, t.created_at)) DESC
        LIMIT ?
        """,
        (RECENT_FILLS_LIMIT,),
    ).fetchall()
    fills = []
    for row in rows:
        net = row["net_pnl_usd"]
        fills.append({
            "id": row["id"],
            "strategy_id": row["strategy_id"],
            "strategy_name": row["strategy_name"],
            "asset": row["asset"],
            "direction": row["direction"],
            "status": str(row["status"] or "").upper(),
            "opened_at": row["opened_at"],
            "closed_at": row["closed_at"],
            "net_pnl_usd": round(float(net), 2) if net is not None else None,
            "close_reason": _close_reason(row["signal_data"]),
            "failure_reason": row["failure_reason"],
        })
    return fills


def build_live_fleet(now: datetime | None = None) -> dict[str, Any]:
    """Scorecard, realized P&L and recent fills for everything trading real money."""
    now = now or datetime.now(timezone.utc)
    with get_db() as conn:
        stale_after = timedelta(
            seconds=max(
                STALE_AFTER_SCAN_INTERVALS * _scan_interval_seconds(conn), MIN_STALE_AFTER_SECONDS
            )
        )
        strategy_rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, name, display_name, symbol, timeframe, stage_changed_at
                FROM strategies WHERE LOWER(COALESCE(stage, status, '')) = ?
                ORDER BY id
                """,
                (LIVE_STAGE,),
            ).fetchall()
        ]
        stats = _trade_stats(conn, [str(row["id"]) for row in strategy_rows])
        strategies = []
        for row in strategy_rows:
            sid = str(row["id"])
            live_since = _parse_ts(row["stage_changed_at"])
            window_start = now - timedelta(days=BLOCK_WINDOW_DAYS)
            if live_since and live_since > window_start:
                window_start = live_since
            blocked = _blocked_entries(conn, sid, window_start)
            last_scan = _last_scan(conn, sid)
            trade_row = stats.get(sid, {})
            open_ids = [tid for tid in str(trade_row.get("open_ids") or "").split(",") if tid]
            state = _strategy_state(
                open_ids=open_ids,
                last_scan_at=_parse_ts(last_scan["at"]) if last_scan else None,
                last_blocked_at=_parse_ts(blocked["last_at"]),
                last_open_at=_parse_ts(trade_row.get("last_open_at")),
                now=now,
                stale_after=stale_after,
            )
            wins = int(trade_row.get("wins") or 0)
            losses = int(trade_row.get("losses") or 0)
            strategies.append({
                "strategy_id": sid,
                "name": row["name"],
                "display_name": row["display_name"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "live_since": _iso(live_since),
                "state": state,
                "open_trade_ids": open_ids,
                "trades": {
                    "closed": int(trade_row.get("closed") or 0),
                    "wins": wins,
                    "losses": losses,
                    "failed": int(trade_row.get("failed") or 0),
                    "win_rate": (wins / (wins + losses)) if wins + losses else None,
                    "net_pnl_usd": round(float(trade_row.get("net_pnl_usd") or 0.0), 2),
                    "last_trade_at": trade_row.get("last_trade_at"),
                },
                "last_scan": last_scan,
                "blocked_entries": {"window_days": BLOCK_WINDOW_DAYS, **blocked},
            })
        realized = _realized(conn, now)
        fills = _recent_fills(conn)
        bots_armed = _live_bots_armed(conn)
    return {
        "generated_at": now.isoformat(),
        "stale_after_seconds": int(stale_after.total_seconds()),
        "strategies": strategies,
        "live_bots_armed": bots_armed,
        "realized": realized,
        "recent_fills": fills,
    }
