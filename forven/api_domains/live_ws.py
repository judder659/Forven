from __future__ import annotations

import asyncio
import time

from fastapi import WebSocket, WebSocketDisconnect

from forven import api_core as core
from forven.async_utils import spawn
from forven.db import get_open_trades


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
WS_TICK_SECONDS = 1.0
WS_PING_INTERVAL_SECONDS = 3.0
WS_SEND_TIMEOUT_SECONDS = 2.5


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

    async def _send_json(payload: dict) -> bool:
        try:
            await asyncio.wait_for(ws.send_json(payload), timeout=WS_SEND_TIMEOUT_SECONDS)
            return True
        except asyncio.TimeoutError:
            core.log.warning("WebSocket send timed out for %s", client_label)
            return False
        except Exception:
            return False

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

    last_prices = daemon.get("last_prices", {})
    last_scan_count = daemon.get("scan_count", 0)
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
