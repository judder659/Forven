from fastapi import APIRouter, Depends
from typing import Dict, Any
import json
import logging
from forven.api_security import require_operator_access
from forven.db import get_db, kv_get
from forven.config import get_execution_mode

log = logging.getLogger("forven.quant_factory")

router = APIRouter(tags=["quant_factory"], prefix="/api/quant-factory", dependencies=[Depends(require_operator_access)])


def _safe_json(raw, fallback=None):
    """Parse JSON string or return dict directly; never raise."""
    if raw is None:
        return fallback if fallback is not None else {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return fallback if fallback is not None else {}


@router.get("/")
def get_quant_factory_data() -> Dict[str, Any]:
    with get_db() as conn:
        conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))

        # ---------- Account & System Status ----------
        daemon = kv_get("daemon_state", {})
        risk_state = kv_get("risk_state", {})
        daily_risk = kv_get("daily_risk", {})

        mode = get_execution_mode()

        # Trading allowed check (lightweight – avoid importing exchange.risk if possible)
        trading_allowed = True
        trading_reason = ""
        try:
            from forven.exchange.risk import is_trading_allowed
            trading_allowed, trading_reason = is_trading_allowed()
        except Exception:
            log.debug("Failed to check trading_allowed", exc_info=True)

        hwm = risk_state.get("high_water_mark", 0)
        drawdown_pct = risk_state.get("drawdown_pct", 0)
        equity = hwm * (1 - drawdown_pct) if hwm > 0 else 0
        account_value = equity or daily_risk.get("start_equity", 0) or daily_risk.get("current_equity", 0)

        # Daily PnL
        start_equity = daily_risk.get("start_equity", 0)
        current_equity = daily_risk.get("current_equity", account_value)
        daily_pnl_usd = current_equity - start_equity if start_equity else 0
        daily_pnl_pct = daily_risk.get("pnl_pct", 0) or (
            (daily_pnl_usd / start_equity * 100) if start_equity else 0
        )

        # Net exposure from open positions
        net_exposure = 0.0
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN direction='long' THEN entry_price * size "
                "WHEN direction='short' THEN -entry_price * size ELSE 0 END), 0) as net "
                "FROM portfolio_positions"
            ).fetchone()
            net_exposure = row["net"] if row else 0
        except Exception:
            log.debug("Failed to compute net exposure", exc_info=True)

        account = {
            "account_value": round(account_value, 2),
            "net_exposure": round(net_exposure, 2),
            "daily_pnl_usd": round(daily_pnl_usd, 2),
            "daily_pnl_pct": round(daily_pnl_pct, 2),
            "execution_mode": mode,
            "trading_allowed": trading_allowed,
            "trading_reason": trading_reason,
            "kill_switch_active": risk_state.get("kill_switch_active", False),
            "daemon_running": daemon.get("running", False),
            "drawdown_pct": round(drawdown_pct * 100 if drawdown_pct < 1 else drawdown_pct, 2),
            "prices": daemon.get("last_prices", {}),
        }

        # ---------- 1. Global Radar ----------
        radar = []
        try:
            # Match both canonical and legacy stage names
            rows = conn.execute(
                "SELECT id, display_id, name, symbol, timeframe, stage, status, metrics, market_pot, model, model_id, updated_at "
                "FROM strategies "
                "WHERE LOWER(TRIM(COALESCE(stage, status, ''))) IN ("
                "  'deployed', 'live_graduated', "
                "  'paper_trading', 'paper', "
                "  'backtesting', 'gauntlet', "
                "  'researching', 'quick_screen'"
                ") "
                "ORDER BY CASE "
                "  WHEN LOWER(TRIM(COALESCE(stage, status, ''))) IN ('deployed', 'live_graduated') THEN 0 "
                "  WHEN LOWER(TRIM(COALESCE(stage, status, ''))) IN ('paper_trading', 'paper') THEN 1 "
                "  WHEN LOWER(TRIM(COALESCE(stage, status, ''))) IN ('backtesting', 'gauntlet') THEN 2 "
                "  ELSE 3 END, "
                "datetime(updated_at) DESC "
                "LIMIT 25"
            ).fetchall()
            # Regime lookup from KV
            regime_cache = {}
            for asset in ("BTC", "ETH", "SOL"):
                cached = kv_get(f"regime:{asset}")
                if cached:
                    regime_cache[asset] = cached.get("regime", "NEU")

            from forven.util import normalize_stage

            for row in rows:
                metrics = _safe_json(row.get("metrics"))
                sharpe = metrics.get("sharpe_ratio", 0)
                total_return = metrics.get("total_return", 0) or metrics.get("total_return_pct", 0)
                monthly_return = metrics.get("monthly_return_pct", total_return)

                # Determine regime from cached data
                symbol = row.get("symbol") or ""
                base_asset = symbol.split("/")[0].upper() if "/" in symbol else symbol.replace("USDT", "").replace("USD", "").upper()
                regime = regime_cache.get(base_asset, row.get("market_pot") or "NEU")

                # Alpha = monthly return (or total return if no monthly)
                alpha_val = monthly_return or total_return
                alpha_str = f"{'+' if alpha_val >= 0 else ''}{alpha_val:.2f}%"

                # Build short model label for display
                raw_model_id = row.get("model_id") or ""
                model_label = raw_model_id.split("/")[-1] if "/" in raw_model_id else raw_model_id
                if not model_label:
                    model_label = row.get("model") or ""

                stage_raw = row.get("stage") or row.get("status")
                stage_canonical = normalize_stage(stage_raw)

                radar.append({
                    "id": row.get("id"),
                    "display_id": row.get("display_id") or None,
                    "strategy_name": row.get("name"),
                    "strategy": row.get("display_id") or row.get("name") or row.get("id") or "N/A",
                    "target": symbol or "BTC/USDT",
                    "timeframe": row.get("timeframe", ""),
                    "regime": regime.upper() if isinstance(regime, str) else "NEU",
                    "stage": stage_canonical,
                    "alpha": alpha_str,
                    "sharpe": round(sharpe, 2) if sharpe else 0,
                    "trend": "up" if alpha_val >= 0 else "down",
                    "model": model_label or None,
                })
        except Exception:
            log.debug("Failed to build radar data", exc_info=True)

        # ---------- 2. Agent Network ----------
        agents = []
        try:
            agent_rows = conn.execute(
                "SELECT id, name, role, enabled, model, updated_at FROM agents ORDER BY name"
            ).fetchall()
            # Get latest task per agent
            task_map: Dict[str, dict] = {}
            try:
                for t in conn.execute(
                    "SELECT agent_id, type, status, title "
                    "FROM agent_tasks "
                    "ORDER BY created_at DESC"
                ).fetchall():
                    aid = t.get("agent_id")
                    if aid and aid not in task_map:
                        task_map[aid] = {
                            "type": t.get("type"),
                            "status": t.get("status"),
                            "title": t.get("title"),
                        }
                # Also check legacy tasks table
                for t in conn.execute(
                    "SELECT payload, status FROM tasks ORDER BY created_at DESC LIMIT 20"
                ).fetchall():
                    payload = _safe_json(t.get("payload"))
                    aid = payload.get("agent_id") or payload.get("agent")
                    if aid and aid not in task_map:
                        task_map[aid] = {
                            "type": payload.get("type", "task"),
                            "status": t.get("status"),
                            "title": payload.get("title", ""),
                        }
            except Exception:
                log.debug("Failed to load agent task map", exc_info=True)

            for ag in agent_rows:
                agent_id = ag.get("id", "")
                task = task_map.get(agent_id, {})
                task_status = task.get("status", "")
                task_title = task.get("title") or task.get("type") or ""

                if task_status in ("running", "in_progress"):
                    status = "active"
                    status_label = task_title or "Working..."
                elif task_status == "pending":
                    status = "pending"
                    status_label = f"Queued: {task_title}" if task_title else "Queued"
                elif not ag.get("enabled"):
                    status = "disabled"
                    status_label = "Disabled"
                else:
                    status = "idle"
                    status_label = "Idle"

                agents.append({
                    "id": agent_id,
                    "name": ag.get("name", agent_id),
                    "role": ag.get("role", ""),
                    "enabled": bool(ag.get("enabled", True)),
                    "model": ag.get("model", ""),
                    "status": status,
                    "status_label": status_label,
                })
        except Exception:
            log.debug("Failed to build agent network", exc_info=True)

        # ---------- 3. Structural Memory Logs ----------
        logs = []
        try:
            for row in conn.execute(
                "SELECT id, level, source, message, created_at "
                "FROM activity_log "
                "ORDER BY id DESC LIMIT 30"
            ).fetchall():
                created = row.get("created_at", "")
                time_part = created.split(" ")[1] if " " in created else created
                source_raw = str(row.get("source") or "system").strip()
                level = str(row.get("level") or "info").lower()

                if source_raw.startswith("agent:"):
                    tag = f"[{source_raw[6:].upper()}]"
                    layer = "brain"
                elif source_raw == "brain":
                    tag = "[BRAIN]"
                    layer = "brain"
                elif source_raw in ("execution", "trader", "execution-trader"):
                    tag = "[EXEC]"
                    layer = "exec"
                elif level == "error":
                    tag = f"[{source_raw.upper()}]"
                    layer = "decay"
                else:
                    tag = f"[{source_raw.upper()}]"
                    layer = "exec"

                logs.append({
                    "id": row.get("id"),
                    "time": time_part,
                    "tag": tag,
                    "layer": layer,
                    "level": level,
                    "msg": row.get("message", ""),
                })
        except Exception:
            log.debug("Failed to build memory logs", exc_info=True)

        # ---------- 4. System Intel ----------
        intel_count = 0
        live_count = 0
        paper_count = 0
        gauntlet_count = 0
        quick_screen_count = 0
        total_trades = 0
        open_trades = 0
        avg_slippage_bps = 0.0
        total_backtests = 0

        try:
            from forven.util import normalize_stage
            all_strats = conn.execute("SELECT stage, status FROM strategies").fetchall()
            intel_count = len(all_strats)
            for s in all_strats:
                st = normalize_stage(s.get("stage") or s.get("status"))
                if st == "live_graduated":
                    live_count += 1
                elif st == "paper":
                    paper_count += 1
                elif st == "gauntlet":
                    gauntlet_count += 1
                elif st == "quick_screen":
                    quick_screen_count += 1
        except Exception:
            log.debug("Failed to query strategy counts", exc_info=True)

        try:
            total_trades = conn.execute("SELECT COUNT(*) as c FROM trades").fetchone()["c"]
            open_trades = conn.execute("SELECT COUNT(*) as c FROM trades WHERE status = 'OPEN'").fetchone()["c"]
        except Exception:
            log.debug("Failed to query trade counts", exc_info=True)

        try:
            slip_row = conn.execute(
                "SELECT AVG(abs_slippage_bps) as avg_slip FROM trade_slippage_audit"
            ).fetchone()
            avg_slippage_bps = round(slip_row["avg_slip"], 2) if slip_row and slip_row["avg_slip"] else 0
        except Exception:
            log.debug("Failed to query slippage stats", exc_info=True)

        try:
            total_backtests = conn.execute("SELECT COUNT(*) as c FROM backtest_runs").fetchone()["c"]
        except Exception:
            log.debug("Failed to query backtest count", exc_info=True)

        intel = {
            "total_strategies": intel_count,
            "live": live_count,
            "paper": paper_count,
            "backtesting": gauntlet_count,
            "researching": quick_screen_count,
            "total_trades": total_trades,
            "open_trades": open_trades,
            "avg_slippage_bps": avg_slippage_bps,
            "total_backtests": total_backtests,
            "agent_count": len(agents),
        }

        # ---------- 5. Arena (champion vs challenger) ----------
        arena = []
        try:
            # Find symbols that have both a deployed and paper_trading strategy
            deployed = {}
            paper = {}
            from forven.util import normalize_stage
            for row in conn.execute(
                "SELECT id, name, symbol, stage, status, metrics FROM strategies "
                "WHERE symbol IS NOT NULL"
            ).fetchall():
                st = normalize_stage(row.get("stage") or row.get("status"))
                if st not in ("live_graduated", "paper"):
                    continue
                    
                m = _safe_json(row.get("metrics"))
                entry = {
                    "id": row["id"],
                    "name": row["name"],
                    "symbol": row["symbol"],
                    "sharpe": m.get("sharpe_ratio", 0),
                    "total_return": m.get("total_return", 0) or m.get("total_return_pct", 0),
                }
                if st == "live_graduated":
                    deployed.setdefault(row["symbol"], []).append(entry)
                else:
                    paper.setdefault(row["symbol"], []).append(entry)

            for symbol in deployed:
                if symbol in paper:
                    champ = max(deployed[symbol], key=lambda x: x.get("sharpe", 0))
                    challenger = max(paper[symbol], key=lambda x: x.get("sharpe", 0))
                    edge = (challenger.get("total_return", 0) or 0) - (champ.get("total_return", 0) or 0)
                    arena.append({
                        "symbol": symbol,
                        "champion": {
                            "name": champ["name"],
                            "sharpe": round(champ.get("sharpe", 0), 2),
                            "return": round(champ.get("total_return", 0), 2),
                        },
                        "challenger": {
                            "name": challenger["name"],
                            "sharpe": round(challenger.get("sharpe", 0), 2),
                            "return": round(challenger.get("total_return", 0), 2),
                        },
                        "edge_pct": round(edge, 2),
                        "threshold_pct": 5.0,
                    })
        except Exception:
            log.debug("Failed to build arena data", exc_info=True)

        # ---------- 6. Validation (latest backtest) ----------
        validation_is = {"trades": 0, "sharpe": 0, "max_dd": 0, "win_rate": 0}
        validation_oos = {"trades": 0, "sharpe": 0, "max_dd": 0, "win_rate": 0}
        robustness = 0.0
        validation_strategy = ""
        degradation_pct = 0.0
        validation_status = "N/A"

        try:
            target_bt = conn.execute(
                "SELECT * FROM backtest_runs ORDER BY datetime(start_time) DESC LIMIT 1"
            ).fetchone()
            if target_bt:
                is_met = _safe_json(target_bt.get("is_metrics"))
                oos_met = _safe_json(target_bt.get("oos_metrics"))
                robustness = float(target_bt.get("robustness_score", 0) or 0)
                validation_strategy = target_bt.get("strategy_name", "") or ""

                validation_is = {
                    "trades": is_met.get("total_trades", 0),
                    "sharpe": round(is_met.get("sharpe_ratio", 0), 2),
                    "max_dd": round(is_met.get("max_drawdown_pct", 0), 1),
                    "win_rate": round(is_met.get("win_rate_pct", 0), 1),
                }
                validation_oos = {
                    "trades": oos_met.get("total_trades", 0),
                    "sharpe": round(oos_met.get("sharpe_ratio", 0), 2),
                    "max_dd": round(oos_met.get("max_drawdown_pct", 0), 1),
                    "win_rate": round(oos_met.get("win_rate_pct", 0), 1),
                }
                # Degradation: how much IS sharpe dropped in OOS
                is_sharpe = is_met.get("sharpe_ratio", 0)
                oos_sharpe = oos_met.get("sharpe_ratio", 0)
                if is_sharpe and is_sharpe > 0:
                    degradation_pct = round((1 - oos_sharpe / is_sharpe) * 100, 1)
                validation_status = "PASSED" if robustness >= 0.7 else "FAILED"
        except Exception:
            log.debug("Failed to build validation data", exc_info=True)

        return {
            "account": account,
            "radar": radar,
            "agents": agents,
            "logs": logs,
            "arena": arena,
            "validation": {
                "is": validation_is,
                "oos": validation_oos,
                "robustness": round(robustness, 2),
                "degradation_pct": degradation_pct,
                "strategy_name": validation_strategy,
                "status": validation_status,
            },
            "intel": intel,
        }
