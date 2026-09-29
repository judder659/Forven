from __future__ import annotations

import asyncio
import json
import time

from fastapi import WebSocket, WebSocketDisconnect

from forven import api_core as core
from forven.async_utils import spawn
from forven.db import get_open_trades
from forven.notification_catalog import category_for_event, normalize_severity
from forven.notifications import is_actionable_notification


class ConnectionManager:
    """Manages active WebSocket connections."""

    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, message: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


ws_manager = ConnectionManager()
# Notification rows that never reach the operator (dedupe hits) are not pushed.
_UNPUSHED_NOTIFICATION_STATUSES = {"suppressed", "dropped"}
# Metadata the pop-up router needs; the rest stays in the Diagnostics inbox.
_PUSHED_NOTIFICATION_METADATA = ("execution_type", "execution_mode", "strategy_id", "trade_id", "asset", "test_notification")
WS_TICK_SECONDS = 1.0
WS_PING_INTERVAL_SECONDS = 3.0
WS_SEND_TIMEOUT_SECONDS = 2.5
# Consecutive send TIMEOUTS tolerated before dropping the client. A timeout is
# usually a transient event-loop/GIL stall (see forven.loop_watchdog), not a dead
# socket — and every outbound message is a full snapshot (prices, pnl, daemon
# state), so skipping one loses nothing. A hard send exception (closed socket)
# still drops immediately. Grace=2 rides out a single storm; persistent
# backpressure (a genuinely wedged client) still disconnects within ~2 ticks.
WS_SEND_TIMEOUT_GRACE = 2


async def websocket_endpoint(ws: WebSocket):
    async def _safe_to_thread(fn, *args, default=None, timeout_seconds: float | None = 2.5):
        try:
            if timeout_seconds is not None and timeout_seconds > 0:
                return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout_seconds)
            return await asyncio.to_thread(fn, *args)
        except asyncio.TimeoutError:
            core.log.debug("WebSocket background read timed out for %s", getattr(fn, "__name__", "callable"))
            return default
        except Exception:
            core.log.debug(
                "WebSocket background read failed for %s",
                getattr(fn, "__name__", "callable"),
                exc_info=True,
            )
            return default

    def _read_max_log_id() -> int:
        with core.get_db() as conn:
            row = conn.execute("SELECT MAX(id) as max_id FROM activity_log").fetchone()
            return int((row["max_id"] or 0) if row else 0)

    def _read_new_logs(since_id: int) -> list[dict]:
        with core.get_db() as conn:
            rows = conn.execute(
                "SELECT * FROM activity_log WHERE id > ? ORDER BY id LIMIT 20",
                (since_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _read_closed_trades(trade_ids: list[str]) -> list[dict]:
        if not trade_ids:
            return []
        placeholders = ",".join("?" for _ in trade_ids)
        with core.get_db() as conn:
            rows = conn.execute(
                "SELECT id, display_id, asset, direction, strategy, execution_type, source, "
                "entry_price, exit_price, pnl_pct, net_pnl_pct, status "
                f"FROM trades WHERE id IN ({placeholders})",
                trade_ids,
            ).fetchall()
        return [dict(r) for r in rows]

    def _read_pending_approvals() -> list[dict]:
        with core.get_db() as conn:
            rows = conn.execute(
                "SELECT id, approval_type, target_type, target_id, requested_status, reason, created_at "
                "FROM approvals WHERE status = 'pending_approval' ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def _read_max_notification_id() -> int:
        with core.get_db() as conn:
            row = conn.execute("SELECT MAX(id) as max_id FROM notifications").fetchone()
            return int((row["max_id"] or 0) if row else 0)

    def _read_new_notifications(since_id: int) -> list[dict]:
        with core.get_db() as conn:
            rows = conn.execute(
                "SELECT id, event_type, severity, source, title, summary, status, metadata, created_at "
                "FROM notifications WHERE id > ? ORDER BY id LIMIT 25",
                (since_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def _notification_event(row: dict) -> dict:
        try:
            metadata = json.loads(row.get("metadata") or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        severity = normalize_severity(row.get("severity"))
        return {
            "id": int(row["id"]),
            "event_type": str(row.get("event_type") or ""),
            "category": category_for_event(row.get("event_type"), severity, metadata),
            "severity": severity,
            "source": row.get("source"),
            "title": row.get("title"),
            "summary": row.get("summary"),
            "status": row.get("status"),
            "created_at": row.get("created_at"),
            "actionable": is_actionable_notification({**row, "severity": severity}),
            "metadata": {key: metadata[key] for key in _PUSHED_NOTIFICATION_METADATA if key in metadata},
        }

    def _trade_summary(trade: dict) -> dict:
        return {
            "id": str(trade.get("id")),
            "display_id": trade.get("display_id"),
            "asset": trade.get("asset"),
            "direction": trade.get("direction"),
            "strategy": trade.get("strategy"),
            "execution_type": trade.get("execution_type"),
            "source": trade.get("source"),
            "entry_price": trade.get("entry_price"),
            "exit_price": trade.get("exit_price"),
            "pnl_pct": trade.get("net_pnl_pct") if trade.get("net_pnl_pct") is not None else trade.get("pnl_pct"),
            "status": trade.get("status"),
        }

    consecutive_send_timeouts = [0]

    async def _send_json(payload: dict) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=WS_SEND_TIMEOUT_SECONDS)
            consecutive_send_timeouts[0] = 0
            return True
        except asyncio.TimeoutError:
            consecutive_send_timeouts[0] += 1
            core.log.warning(
                "WebSocket send timed out for %s (%d/%d consecutive before drop)",
                client_label, consecutive_send_timeouts[0], WS_SEND_TIMEOUT_GRACE,
            )
            # Transient loop/GIL stall: skip this snapshot, keep the client.
            # Worst case the cancelled send broke the framing — the NEXT send
            # then raises and the hard-exception path below drops immediately.
            return consecutive_send_timeouts[0] < WS_SEND_TIMEOUT_GRACE
        except Exception:
            return False  # closed/broken socket — no grace

    async def _send_messages(payloads: list[dict]) -> bool:
        combined = core._coalesce_ws_messages(payloads)
        if combined is None:
            return True
        return await _send_json(combined)

    async def _drain_client_messages() -> int | None:
        """Consume incoming client frames to avoid receive-buffer buildup."""
        try:
            while True:
                msg = await ws.receive()
                msg_type = str(msg.get("type") or "")
                if msg_type == "websocket.disconnect":
                    code = msg.get("code")
                    try:
                        return int(code) if code is not None else None
                    except Exception:
                        return None
                # Ignore app-level messages (e.g., client pong).
        except WebSocketDisconnect as exc:
            try:
                return int(getattr(exc, "code", None)) if getattr(exc, "code", None) is not None else None
            except Exception:
                return None
        except Exception:
            core.log.debug("WebSocket receive loop aborted", exc_info=True)
            return None

    # SECURITY (audit 2026-06-22, L3): WS handshakes bypass ApiKeyMiddleware
    # (non-http scope), so authorize here. Fail-open when no key is set (default
    # localhost); enforce the key once one is configured (e.g. exposed bind).
    # ws-no-origin-check: require_api_access_ws ALSO rejects a cross-site Origin
    # now — CsrfOriginMiddleware likewise never sees this scope, and a WS upgrade
    # has no preflight, so a browsed page could otherwise stream live positions.
    from forven.api_security import require_api_access_ws

    if not await require_api_access_ws(ws):
        return

    await ws_manager.connect(ws)
    client = getattr(ws, "client", None)
    client_label = f"{getattr(client, 'host', 'unknown')}:{getattr(client, 'port', '')}".rstrip(":")
    core.log.info("WebSocket connected: %s", client_label)

    daemon = await _safe_to_thread(core.kv_get, "daemon_state", {}, default={}, timeout_seconds=2.5) or {}
    if not await _send_messages([{"type": "init", "data": daemon}]):
        ws_manager.disconnect(ws)
        return

    receiver_task = spawn(_drain_client_messages(), name="ws-client-receiver")
    last_log_id = await _safe_to_thread(_read_max_log_id, default=0, timeout_seconds=2.5)
    # New-notification baseline (None until read: never replay history on
    # connect, and a failed read must not fake a burst of "new" rows).
    last_notification_id: int | None = await _safe_to_thread(
        _read_max_notification_id, default=None, timeout_seconds=2.5
    )

    last_prices = daemon.get("last_prices", {})
    last_scan_count = daemon.get("scan_count", 0)
    # Open-trade snapshot (id → row) for push-on-change trade events (None until
    # first read — never emit a spurious diff against an unknown baseline on
    # connect). Rows are kept so a close event can still describe the trade even
    # if the DB lookup for the final row fails.
    last_open_trades: dict[str, dict] | None = None
    # Pending-approval id snapshot for push-on-change approval events (same
    # None-baseline rule as trades).
    last_pending_approval_ids: set[int] | None = None
    risk_state_boot = await _safe_to_thread(core.kv_get, "risk_state", {}, default={}, timeout_seconds=2.5) or {}
    last_kill_switch_state = bool(risk_state_boot.get("kill_switch_active", False))
    last_daily_halt_state = bool(risk_state_boot.get("daily_loss_halt", False))
    last_risk_drawdown_bucket = -1

    tick_seconds = WS_TICK_SECONDS
    ping_interval_seconds = WS_PING_INTERVAL_SECONDS
    last_ping_sent = time.monotonic()

    try:
        while True:
            if receiver_task.done():
                break

            await asyncio.sleep(tick_seconds)

            now_monotonic = time.monotonic()
            if now_monotonic - last_ping_sent >= ping_interval_seconds:
                if not await _send_messages([{"type": "ping", "ts": core._now()}]):
                    break
                last_ping_sent = now_monotonic

            current_daemon = await _safe_to_thread(core.kv_get, "daemon_state", {}, default={}, timeout_seconds=2.5) or {}
            current_prices = current_daemon.get("last_prices", {})
            current_scan = current_daemon.get("scan_count", 0)

            if current_prices != last_prices:
                outbound_messages = [{"type": "prices", "prices": current_prices}]
                last_prices = current_prices

                open_trades = await _safe_to_thread(get_open_trades, default=[], timeout_seconds=2.5) or []
                # Push-on-change trade events: an id appearing/disappearing from the OPEN
                # set means a fill happened (scanner sweep, mark watcher, manual) —
                # whichever PROCESS did it, the DB is the shared truth. The frontend
                # routes type 'trade' to forven:event and refreshes the session, trades
                # table and chart markers within ~1s instead of its 10-15s pollers.
                # Entries carry trade summaries (asset/direction/strategy/pnl) so the
                # notification layer can toast without a follow-up fetch.
                current_open_trades = {str(t.get("id")): t for t in open_trades}
                if last_open_trades is not None and set(current_open_trades) != set(last_open_trades):
                    opened_ids = sorted(set(current_open_trades) - set(last_open_trades))
                    closed_ids = sorted(set(last_open_trades) - set(current_open_trades))
                    # Closed rows left the open set, so re-read them for exit price/pnl;
                    # fall back to the stale open-row snapshot if the read fails.
                    closed_rows = {
                        str(row.get("id")): row
                        for row in (
                            await _safe_to_thread(_read_closed_trades, closed_ids, default=[], timeout_seconds=2.5)
                            or []
                        )
                    }
                    outbound_messages.append(
                        {
                            "type": "trade",
                            "data": {
                                "opened": [_trade_summary(current_open_trades[tid]) for tid in opened_ids],
                                "closed": [
                                    _trade_summary(closed_rows.get(tid) or last_open_trades[tid])
                                    for tid in closed_ids
                                ],
                            },
                        }
                    )
                last_open_trades = current_open_trades
                if open_trades:
                    pnl_updates = []
                    for trade in open_trades:
                        current_price = current_prices.get(trade["asset"])
                        if current_price and trade["entry_price"]:
                            direction = (trade.get("direction") or "long").lower()
                            if direction == "long":
                                pct = (float(current_price) - trade["entry_price"]) / trade["entry_price"]
                            else:
                                pct = (trade["entry_price"] - float(current_price)) / trade["entry_price"]
                            pnl_updates.append(
                                {
                                    "id": trade["id"],
                                    "asset": trade["asset"],
                                    "pnl_pct": round(pct, 6),
                                    "current_price": float(current_price),
                                }
                            )
                    if pnl_updates:
                        outbound_messages.append({"type": "position_pnl", "positions": pnl_updates})
                if not await _send_messages(outbound_messages):
                    break

            if current_scan != last_scan_count:
                last_scan_count = current_scan

            current_risk_state = await _safe_to_thread(core.kv_get, "risk_state", {}, default={}, timeout_seconds=2.5) or {}
            current_kill_switch_state = bool(current_risk_state.get("kill_switch_active", False))
            if current_kill_switch_state != last_kill_switch_state:
                last_kill_switch_state = current_kill_switch_state
                kill_switch_ts = core._now()
                if not await _send_messages(
                    [
                        {
                            "type": "kill_switch_activated" if current_kill_switch_state else "kill_switch_cleared",
                            "data": {
                                "kill_switch_active": current_kill_switch_state,
                                "ts": kill_switch_ts,
                            },
                        },
                        {
                            "type": "risk_alert",
                            "data": {
                                "kind": "kill_switch",
                                "kill_switch_active": current_kill_switch_state,
                                "ts": kill_switch_ts,
                            },
                        },
                    ]
                ):
                    break

            current_daily_halt_state = bool(current_risk_state.get("daily_loss_halt", False))
            if current_daily_halt_state != last_daily_halt_state:
                last_daily_halt_state = current_daily_halt_state
                if not await _send_messages(
                    [
                        {
                            "type": "risk_alert",
                            "data": {
                                "kind": "daily_loss_halt",
                                "daily_loss_halt": current_daily_halt_state,
                                "ts": core._now(),
                            },
                        }
                    ]
                ):
                    break

            risk_snapshot = current_daemon.get("risk", {}) if isinstance(current_daemon, dict) else {}
            drawdown_pct = core._coerce_float((risk_snapshot or {}).get("drawdown_pct"), 0.0)
            if drawdown_pct >= 0.08:
                bucket = int(drawdown_pct * 100)
                if bucket != last_risk_drawdown_bucket:
                    last_risk_drawdown_bucket = bucket
                    if not await _send_messages(
                        [
                            {
                                "type": "risk_alert",
                                "data": {
                                    "kind": "drawdown_warning",
                                    "drawdown_pct": drawdown_pct,
                                    "ts": core._now(),
                                },
                            }
                        ]
                    ):
                        break

            # Push-on-change approval events: a new pending approval means the
            # operator's attention is required NOW — the frontend toasts it and
            # badges the Approvals tab instead of waiting for the next heartbeat.
            # default=None keeps a failed/timed-out read from faking resolutions.
            pending_approvals = await _safe_to_thread(_read_pending_approvals, default=None, timeout_seconds=2.5)
            if pending_approvals is not None:
                current_pending = {int(item["id"]): item for item in pending_approvals}
                if last_pending_approval_ids is not None and set(current_pending) != last_pending_approval_ids:
                    approval_messages: list[dict] = [
                        {"type": "approval_created", "data": current_pending[approval_id]}
                        for approval_id in sorted(set(current_pending) - last_pending_approval_ids)
                    ]
                    resolved_ids = sorted(last_pending_approval_ids - set(current_pending))
                    if resolved_ids:
                        approval_messages.append({"type": "approval_resolved", "data": {"ids": resolved_ids}})
                    if not await _send_messages(approval_messages):
                        break
                last_pending_approval_ids = set(current_pending)

            # Push-on-change notification events: every stored notification the
            # operator would see (dedupe hits excluded), tagged with its catalog
            # category. The frontend pops up the categories the operator has
            # switched on and refreshes the Diagnostics badge on actionable ones.
            if last_notification_id is None:
                last_notification_id = await _safe_to_thread(
                    _read_max_notification_id, default=None, timeout_seconds=2.5
                )
            else:
                new_notifications = await _safe_to_thread(
                    _read_new_notifications, int(last_notification_id), default=None, timeout_seconds=2.5
                )
                if new_notifications:
                    last_notification_id = int(new_notifications[-1]["id"])
                    notification_messages = [
                        {"type": "notification", "data": _notification_event(row)}
                        for row in new_notifications
                        if str(row.get("status") or "").strip().lower() not in _UNPUSHED_NOTIFICATION_STATUSES
                    ]
                    if notification_messages and not await _send_messages(notification_messages):
                        break

            entries = await _safe_to_thread(_read_new_logs, int(last_log_id or 0), default=[], timeout_seconds=2.5) or []
            if entries:
                last_log_id = entries[-1]["id"]
                outbound_messages = [{"type": "logs", "entries": entries}]
                for entry in entries:
                    if entry.get("level") == "trade":
                        outbound_messages.append({"type": "trade", "data": entry})
                    mapped = core._classify_activity_log_event(entry)
                    if mapped:
                        outbound_messages.append({"type": "event", "event": mapped, "data": entry})
                        outbound_messages.append({"type": mapped, "data": entry})
                        if mapped in {"task_queued", "task_completed", "task_failed"}:
                            outbound_messages.append({"type": "event", "event": "task_status_changed", "data": entry})
                            outbound_messages.append({"type": "task_status_changed", "data": entry})
                if not await _send_messages(outbound_messages):
                    break
    except WebSocketDisconnect:
        pass
    except Exception:
        core.log.exception("WebSocket loop crashed for %s", client_label)
    finally:
        disconnect_code = None
        if receiver_task.done():
            try:
                disconnect_code = receiver_task.result()
            except Exception:
                disconnect_code = None
        else:
            receiver_task.cancel()
            try:
                await receiver_task
            except Exception:
                pass
        ws_manager.disconnect(ws)
        if disconnect_code is None:
            core.log.info("WebSocket disconnected: %s", client_label)
        else:
            core.log.info("WebSocket disconnected: %s (code=%s)", client_label, disconnect_code)


__all__ = [
    "ConnectionManager",
    "websocket_endpoint",
    "ws_manager",
]
