"""Fleet scorecards: what each live (or paper) strategy is doing right now.

Built only from the local database (no exchange calls), so it stays cheap on the
single-worker API. Live means ``execution_type = 'live'`` trade rows and
strategies at the ``live_graduated`` stage; paper means ``execution_type =
'paper'`` rows and strategies whose stage starts with ``paper`` (the same filter
the paper sessions use). The global ``execution_mode`` setting is a label only;
a strategy's stage decides whether it trades real money.

Refused entries and exits come from ``scanner_signal_results``. The scanner
writes an ``evaluation_only`` placeholder for every matched signal before
execution, and ``no_actionable_position_or_order`` when a signal simply
continues (or has nothing to close on) the current position. Neither is a
refusal, so both are excluded along with pre-outcome rows that carry no reason.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from forven.db import get_db
from forven.trade_accounting import net_pnl_sql

log = logging.getLogger("forven.api")

LIVE_STAGE = "live_graduated"
FLEET_MODES = ("live", "paper")
_STAGE_FILTERS = {
    "live": ("LOWER(COALESCE(stage, status, '')) = ?", LIVE_STAGE),
    "paper": ("LOWER(COALESCE(stage, status, '')) LIKE ?", "paper%"),
}
BLOCK_WINDOW_DAYS = 30
# A blocked entry keeps a strategy in the BLOCKED state until a later entry
# fills, or until the block is this old.
BLOCKED_STATE_MAX_AGE = timedelta(days=7)
DEFAULT_SCAN_INTERVAL_SECONDS = 300
# Healthy evaluation gaps reach ~15 min on a 5-minute cadence (observed 2026-09-25),
# so "not scanned" needs a wide margin to avoid false alarms on open positions.
STALE_AFTER_SCAN_INTERVALS = 6
MIN_STALE_AFTER_SECONDS = 1800
RECENT_FILLS_LIMIT = 10
NOT_BLOCKS = ("", "evaluation_only", "no_actionable_position_or_order", "no_signal")
_NOT_BLOCKS = NOT_BLOCKS
_SCANNER_TS_FORMAT = "%Y-%m-%dT%H:%M:%S+00:00"
_NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# Out-of-sample backtest fields the trading desk compares live results against.
_BACKTEST_KEYS = (
    "total_trades", "wins", "losses", "win_rate", "profit_factor", "avg_trade_pct",
    "avg_bars_held", "backtest_months", "sharpe", "max_drawdown_pct", "total_return_pct",
    "start_date", "end_date",
)


def _parse_ts(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _check_mode(mode: str) -> str:
    normalized = str(mode or "").strip().lower()
    if normalized not in FLEET_MODES:
        raise ValueError(f"mode must be one of {FLEET_MODES}, got {mode!r}")
    return normalized


def fleet_strategy_rows(conn: Any, mode: str) -> list[dict[str, Any]]:
    """Strategies at the stage that trades in ``mode``."""
    clause, value = _STAGE_FILTERS[_check_mode(mode)]
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT id, name, display_name, symbol, timeframe, stage_changed_at, metrics
            FROM strategies WHERE {clause}
            ORDER BY id
            """,
            (value,),
        ).fetchall()
    ]


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


def _trade_stats(conn: Any, strategy_ids: list[str], execution_type: str = "live") -> dict[str, dict[str, Any]]:
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
        WHERE LOWER(COALESCE(execution_type, '')) = ?
          AND COALESCE(strategy_id, strategy) IN ({placeholders})
        GROUP BY sid
        """,
        (execution_type, *strategy_ids),
    ).fetchall()
    return {str(row["sid"]): dict(row) for row in rows}


def trade_intervals(conn: Any, strategy_id: str, execution_type: str) -> list[tuple[datetime, datetime | None]]:
    """(opened, closed-or-None) for every trade that actually held a position."""
    rows = conn.execute(
        """
        SELECT opened_at, closed_at, status FROM trades
        WHERE COALESCE(strategy_id, strategy) = ? AND LOWER(COALESCE(execution_type, '')) = ?
          AND UPPER(COALESCE(status, '')) != 'FAILED' AND opened_at IS NOT NULL
        """,
        (strategy_id, execution_type),
    ).fetchall()
    out: list[tuple[datetime, datetime | None]] = []
    for row in rows:
        opened = _parse_ts(row["opened_at"])
        if opened is None:
            continue
        closed = _parse_ts(row["closed_at"]) if str(row["status"] or "").upper() != "OPEN" else None
        out.append((opened, closed))
    return out


def held_at(intervals: list[tuple[datetime, datetime | None]], at: datetime | None) -> bool:
    """True when a position was open at ``at``."""
    if at is None:
        return False
    return any(opened <= at and (closed is None or at <= closed) for opened, closed in intervals)


def _reason_key(reason: str) -> str:
    return _NUMBER_RE.sub("#", reason)


def _refusals(conn: Any, strategy_id: str, since: datetime, signal_type: str) -> list[Any]:
    placeholders = ",".join("?" for _ in NOT_BLOCKS)
    return conn.execute(
        f"""
        SELECT ts, block_reason FROM scanner_signal_results
        WHERE strategy_id = ? AND ts >= ?
          AND signal_type = ? AND matched = 1 AND executed = 0
          AND COALESCE(block_reason, '') NOT IN ({placeholders})
        ORDER BY ts DESC
        """,
        (strategy_id, since.strftime(_SCANNER_TS_FORMAT), signal_type, *NOT_BLOCKS),
    ).fetchall()


def _blocked_entries(conn: Any, strategy_id: str, since: datetime) -> dict[str, Any]:
    rows = _refusals(conn, strategy_id, since, "entry")
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


def _blocked_exits(
    conn: Any,
    strategy_id: str,
    since: datetime,
    intervals: list[tuple[datetime, datetime | None]],
) -> dict[str, Any]:
    """Refused exit signals, split by whether a position was open at the time.

    A refused exit while flat is harmless noise; the same refusal on an open
    position means the strategy wanted out and could not get out.
    """
    rows = _refusals(conn, strategy_id, since, "exit")
    summary: dict[str, Any] = {
        "count": len(rows),
        "positioned_count": 0,
        "last_at": None,
        "last_reason": None,
        "last_positioned_at": None,
        "last_positioned_reason": None,
    }
    if not rows:
        return summary
    summary["last_at"] = rows[0]["ts"]
    summary["last_reason"] = rows[0]["block_reason"]
    for row in rows:
        if not held_at(intervals, _parse_ts(row["ts"])):
            continue
        summary["positioned_count"] += 1
        if summary["last_positioned_at"] is None:
            summary["last_positioned_at"] = row["ts"]
            summary["last_positioned_reason"] = row["block_reason"]
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
    stuck_exit_at: datetime | None = None,
) -> str:
    # Staleness wins: an open position the scanner no longer evaluates cannot exit.
    if last_scan_at is None or now - last_scan_at > stale_after:
        return "stale"
    if open_ids:
        # An exit refused after the open position was entered: it wanted out and couldn't.
        if stuck_exit_at is not None and (last_open_at is None or stuck_exit_at >= last_open_at):
            return "exit_blocked"
        return "in_position"
    if (
        last_blocked_at is not None
        and now - last_blocked_at <= BLOCKED_STATE_MAX_AGE
        and (last_open_at is None or last_blocked_at > last_open_at)
    ):
        return "blocked"
    return "watching"


def _backtest_oos(metrics_blob: object) -> dict[str, Any] | None:
    try:
        metrics = json.loads(metrics_blob) if isinstance(metrics_blob, str) else metrics_blob
    except (TypeError, ValueError):
        return None
    if not isinstance(metrics, dict):
        return None
    oos = metrics.get("out_of_sample")
    if not isinstance(oos, dict) or not oos:
        return None
    return {key: oos.get(key) for key in _BACKTEST_KEYS}


def _scope(strategy_ids: list[str] | None) -> tuple[str, tuple[Any, ...]]:
    if strategy_ids is None:
        return "", ()
    if not strategy_ids:
        return " AND 1 = 0", ()
    placeholders = ",".join("?" for _ in strategy_ids)
    return f" AND COALESCE(strategy_id, strategy) IN ({placeholders})", tuple(strategy_ids)


def _realized(
    conn: Any,
    now: datetime,
    execution_type: str = "live",
    strategy_ids: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    pnl = net_pnl_sql()
    windows = {"7d": now - timedelta(days=7), "30d": now - timedelta(days=30), "all": None}
    scope_sql, scope_params = _scope(strategy_ids)
    out: dict[str, dict[str, Any]] = {}
    for label, since in windows.items():
        where = (
            "LOWER(COALESCE(execution_type, '')) = ? "
            "AND UPPER(COALESCE(status, '')) = 'CLOSED' AND closed_at IS NOT NULL"
            + scope_sql
        )
        params: tuple[Any, ...] = (execution_type, *scope_params)
        if since is not None:
            where += " AND datetime(closed_at) >= datetime(?)"
            params = (*params, since.isoformat())
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


def _recent_fills(
    conn: Any,
    execution_type: str = "live",
    strategy_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    pnl = net_pnl_sql()
    scope_sql, scope_params = _scope(strategy_ids)
    scope_sql = scope_sql.replace("COALESCE(strategy_id, strategy)", "COALESCE(t.strategy_id, t.strategy)")
    rows = conn.execute(
        f"""
        SELECT t.id, COALESCE(t.strategy_id, t.strategy) AS strategy_id,
          COALESCE(NULLIF(s.display_name, ''), s.name, t.strategy_name) AS strategy_name,
          t.asset, t.direction, t.status, t.opened_at, t.closed_at, t.failure_reason,
          t.signal_data,
          CASE WHEN UPPER(COALESCE(t.status, '')) = 'CLOSED' THEN ({pnl}) END AS net_pnl_usd
        FROM trades t
        LEFT JOIN strategies s ON s.id = COALESCE(t.strategy_id, t.strategy)
        WHERE LOWER(COALESCE(t.execution_type, '')) = ?{scope_sql}
        ORDER BY datetime(COALESCE(t.closed_at, t.opened_at, t.created_at)) DESC
        LIMIT ?
        """,
        (execution_type, *scope_params, RECENT_FILLS_LIMIT),
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


def build_fleet(mode: str = "live", now: datetime | None = None) -> dict[str, Any]:
    """Scorecard, realized P&L and recent fills for every strategy trading in ``mode``."""
    mode = _check_mode(mode)
    now = now or datetime.now(timezone.utc)
    with get_db() as conn:
        stale_after = timedelta(
            seconds=max(
                STALE_AFTER_SCAN_INTERVALS * _scan_interval_seconds(conn), MIN_STALE_AFTER_SECONDS
            )
        )
        strategy_rows = fleet_strategy_rows(conn, mode)
        strategy_ids = [str(row["id"]) for row in strategy_rows]
        stats = _trade_stats(conn, strategy_ids, mode)
        strategies = []
        for row in strategy_rows:
            sid = str(row["id"])
            stage_since = _parse_ts(row["stage_changed_at"])
            window_start = now - timedelta(days=BLOCK_WINDOW_DAYS)
            if stage_since and stage_since > window_start:
                window_start = stage_since
            intervals = trade_intervals(conn, sid, mode)
            blocked = _blocked_entries(conn, sid, window_start)
            blocked_exits = _blocked_exits(conn, sid, window_start, intervals)
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
                stuck_exit_at=_parse_ts(blocked_exits["last_positioned_at"]),
            )
            wins = int(trade_row.get("wins") or 0)
            losses = int(trade_row.get("losses") or 0)
            strategies.append({
                "strategy_id": sid,
                "name": row["name"],
                "display_name": row["display_name"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "live_since": _iso(stage_since),
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
                "blocked_exits": {"window_days": BLOCK_WINDOW_DAYS, **blocked_exits},
                "backtest_oos": _backtest_oos(row.get("metrics")),
            })
        # Paper history includes long-archived strategies; scope paper totals to
        # the current paper book so the rollup matches the sessions on screen.
        scope_ids = strategy_ids if mode == "paper" else None
        realized = _realized(conn, now, mode, scope_ids)
        fills = _recent_fills(conn, mode, scope_ids)
        bots_armed = _live_bots_armed(conn) if mode == "live" else 0
        capacity = None
        if mode == "live":
            try:
                from forven.exchange.risk import live_capacity_report

                capacity = live_capacity_report(conn)
            except Exception as exc:  # noqa: BLE001 — the scorecard must still load
                log.warning("Live fleet: capacity report failed: %s", exc)
    return {
        "mode": mode,
        "generated_at": now.isoformat(),
        "stale_after_seconds": int(stale_after.total_seconds()),
        "strategies": strategies,
        "live_bots_armed": bots_armed,
        "realized": realized,
        "recent_fills": fills,
        "capacity": capacity,
    }


def build_live_fleet(now: datetime | None = None) -> dict[str, Any]:
    """Scorecard, realized P&L and recent fills for everything trading real money."""
    return build_fleet("live", now=now)


def build_paper_fleet(now: datetime | None = None) -> dict[str, Any]:
    """The same scorecard for the strategies trading on simulated paper books."""
    return build_fleet("paper", now=now)
