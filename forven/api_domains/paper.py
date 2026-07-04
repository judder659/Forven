import json
import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException

from forven import api_core as core
from forven.api_domains import trading as trading_domain
from forven.db import _now, get_db, kv_get, kv_set, live_equity_baseline_kv_key
from forven.market_data import fetch_market_candles
from forven.scheduler import enable_job
from forven.trade_state import parse_trade_signal_data

log = logging.getLogger("forven.api")


def _parse_strategy_params(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _strategy_trade_keys(strategy_row: dict) -> set[str]:
    keys: set[str] = set()
    for field in ("id", "display_id", "name"):
        normalized = str(strategy_row.get(field) or "").strip().lower()
        if normalized:
            keys.add(normalized)
    return keys


def _matches_strategy_trade(trade_row: dict, strategy_keys: set[str]) -> bool:
    if not strategy_keys:
        return False
    for field in ("strategy_id", "strategy"):
        normalized = str(trade_row.get(field) or "").strip().lower()
        if normalized and normalized in strategy_keys:
            return True
    return False


def _normalize_trade_percent_value(value: object) -> float | None:
    parsed = trading_domain._coerce_optional_float(value)
    if parsed is None:
        return None
    # trade_state.py stores PnL as decimal fraction (e.g., 0.05 for 5%), so always convert to percentage
    return parsed * 100.0


def _coerce_price_map_value(price_map: dict, key: str) -> float | None:
    parsed = trading_domain._coerce_optional_float(price_map.get(key))
    if parsed is None or parsed <= 0:
        return None
    return parsed


def _resolve_session_current_price(price_map: dict, symbol: str) -> float | None:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return None

    direct = _coerce_price_map_value(price_map, normalized_symbol)
    if direct is not None:
        return direct

    asset_key = trading_domain._normalize_asset_key(normalized_symbol)
    if asset_key:
        for candidate in (asset_key, f"{asset_key}/USDT", f"{asset_key}-USDT", f"{asset_key}USDT"):
            match = _coerce_price_map_value(price_map, candidate)
            if match is not None:
                return match

        for raw_key, raw_value in price_map.items():
            if trading_domain._normalize_asset_key(raw_key) != asset_key:
                continue
            parsed = trading_domain._coerce_optional_float(raw_value)
            if parsed is not None and parsed > 0:
                return parsed
    return None


def _build_session_position_view(
    active_trade: dict,
    *,
    current_price: float,
    fallback_time: str,
    exchange_positions: dict | None = None,
) -> tuple[dict | None, float]:
    entry_price = (
        trading_domain._coerce_optional_float(active_trade.get("entry_price"))
        or trading_domain._coerce_optional_float(active_trade.get("fill_entry_price"))
        or trading_domain._coerce_optional_float(active_trade.get("signal_entry_price"))
        or current_price
    )
    size = trading_domain._coerce_optional_float(active_trade.get("size")) or 0.0
    leverage = trading_domain._coerce_optional_float(active_trade.get("leverage")) or 1.0
    direction = trading_domain._normalize_trade_direction(active_trade.get("direction"))
    active_trade_signal_data = parse_trade_signal_data(active_trade.get("signal_data"))
    signed = 1.0 if direction == "long" else -1.0
    if entry_price > 0 and size > 0:
        # PAPER-1: dollar PnL is price_move * size (the size already reflects the
        # leveraged notional); multiplying by leverage again double-counts it and
        # overstates the figure vs the realized close path (trade_state multiplier
        # 1.0). Leverage belongs only in the return-on-margin PERCENT below.
        unrealized_pnl = (current_price - entry_price) * size * signed
        unrealized_pnl_pct = ((current_price - entry_price) / entry_price) * signed * leverage * 100.0
    else:
        unrealized_pnl = trading_domain._coerce_optional_float(active_trade.get("pnl_usd")) or 0.0
        unrealized_pnl_pct = _normalize_trade_percent_value(active_trade.get("pnl_pct")) or 0.0

    # LIVE positions: prefer the EXCHANGE's reported unrealized PnL (and entry) so the
    # live card reconciles to Hyperliquid (it folds in funding/fees the local estimate
    # omits). Gated to genuine live trades and matched by asset+direction so a paper
    # position on the same coin can never pick up a live strategy's exchange figure.
    # Falls back to the local estimate above when no snapshot/match exists.
    is_live_trade = str(active_trade.get("execution_type") or "").strip().lower() == "live"
    if is_live_trade and isinstance(exchange_positions, dict) and exchange_positions:
        asset_key = trading_domain._normalize_asset_key(active_trade.get("asset"))
        match = exchange_positions.get(f"{asset_key}:{direction}") if asset_key else None
        if isinstance(match, dict):
            exch_upnl = trading_domain._coerce_optional_float(match.get("unrealized_pnl"))
            exch_entry = trading_domain._coerce_optional_float(match.get("entry_price"))
            if exch_upnl is not None:
                unrealized_pnl = exch_upnl
                basis_entry = exch_entry if (exch_entry and exch_entry > 0) else entry_price
                if basis_entry > 0 and size > 0:
                    # Return on margin: exchange $PnL relative to the position's margin
                    # (notional / leverage), so the % stays consistent with the $ figure.
                    margin = (basis_entry * size) / max(leverage, 1e-9)
                    unrealized_pnl_pct = (exch_upnl / margin) * 100.0 if margin > 0 else unrealized_pnl_pct

    return (
        {
            "id": str(active_trade.get("id") or ""),
            "side": direction,
            "entry_price": entry_price,
            "entry_time": str(active_trade.get("opened_at") or fallback_time),
            "size": size,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            # Prefer the absolute *_price keys (written by manual SL/TP edits and the
            # scanner's auto-trigger) over the legacy stop_loss/take_profit keys.
            "stop_loss_price": trading_domain._coerce_optional_float(
                active_trade_signal_data.get("stop_loss_price")
                if active_trade_signal_data.get("stop_loss_price") is not None
                else active_trade_signal_data.get("stop_loss")
            ),
            "take_profit_price": trading_domain._coerce_optional_float(
                active_trade_signal_data.get("take_profit_price")
                if active_trade_signal_data.get("take_profit_price") is not None
                else active_trade_signal_data.get("take_profit")
            ),
            "stop_loss_source": str(active_trade_signal_data.get("stop_loss_source") or "").strip() or None,
            "take_profit_source": str(active_trade_signal_data.get("take_profit_source") or "").strip() or None,
            # Manual-control surface: lets the UI show pause state and gate controls.
            "manual_pause": bool(active_trade_signal_data.get("manual_pause")),
            "source": str(
                active_trade_signal_data.get("source") or active_trade.get("source") or ""
            ).strip() or None,
            # Direction book (Approach C sub-account) a live position routes to.
            "book": str(active_trade.get("book") or "").strip() or None,
        },
        unrealized_pnl,
    )


def _build_net_position_view(positions: list[dict], *, current_price: float) -> dict | None:
    if not positions:
        return None
    gross_long_size = sum(float(pos.get("size") or 0.0) for pos in positions if str(pos.get("side") or "").lower() == "long")
    gross_short_size = sum(float(pos.get("size") or 0.0) for pos in positions if str(pos.get("side") or "").lower() == "short")
    net_size = gross_long_size - gross_short_size
    return {
        "sides": [str(pos.get("side") or "long").lower() for pos in positions],
        "gross_long_size": gross_long_size,
        "gross_short_size": gross_short_size,
        "net_size": net_size,
        "current_price": current_price,
        "unrealized_pnl": sum(float(pos.get("unrealized_pnl") or 0.0) for pos in positions),
        "unrealized_pnl_pct": sum(float(pos.get("unrealized_pnl_pct") or 0.0) for pos in positions),
        "position_count": len(positions),
    }


def _to_paper_session_status(stage_value: object, status_value: object) -> str:
    normalized = str(stage_value or status_value or "").strip().lower()
    if not normalized:
        normalized = str(status_value or "").strip().lower()
    if not normalized:
        return "watching"
    if normalized.startswith("warm"):
        return "warming_up"
    if normalized.startswith("replay"):
        return "replay_finished" if "finish" in normalized else "watching"
    if normalized.startswith("stop") or normalized in {"paused", "inactive"}:
        return "stopped"
    if normalized.startswith("deploy") or normalized.startswith("paper"):
        return "watching"
    return "watching"


def _build_compat_paper_trade(trade_row: dict, strategy_name: str, symbol: str) -> dict:
    signal_data = parse_trade_signal_data(trade_row.get("signal_data"))
    entry_price = trading_domain._coerce_optional_float(trade_row.get("entry_price"))
    if entry_price is None:
        entry_price = trading_domain._coerce_optional_float(trade_row.get("fill_entry_price"))
    if entry_price is None:
        entry_price = trading_domain._coerce_optional_float(trade_row.get("signal_entry_price"))
    if entry_price is None:
        entry_price = 0.0

    exit_price = trading_domain._coerce_optional_float(trade_row.get("exit_price"))
    if exit_price is None:
        exit_price = trading_domain._coerce_optional_float(trade_row.get("fill_exit_price"))
    if exit_price is None:
        exit_price = trading_domain._coerce_optional_float(trade_row.get("signal_exit_price"))

    pnl = trading_domain._coerce_optional_float(trade_row.get("pnl_usd"))
    pnl_pct = _normalize_trade_percent_value(trade_row.get("pnl_pct"))
    size = trading_domain._coerce_optional_float(trade_row.get("size")) or 0.0
    leverage = trading_domain._coerce_optional_float(trade_row.get("leverage")) or 1.0
    direction = trading_domain._normalize_trade_direction(trade_row.get("direction"))
    exit_time = str(trade_row.get("closed_at") or "").strip() or None
    close_reason = str(signal_data.get("close_reason") or "").strip() or None
    close_incomplete = bool(signal_data.get("close_incomplete")) or (
        exit_time is not None
        and exit_price is None
        and pnl is None
        and pnl_pct is None
    )
    if exit_price is not None and entry_price > 0:
        signed = 1.0 if direction == "long" else -1.0
        if pnl_pct is None:
            pnl_pct = ((exit_price - entry_price) / entry_price) * signed * leverage * 100.0
        if pnl is None and size > 0:
            # PAPER-1: dollar PnL excludes the leverage multiplier (see above) so
            # it matches the realized close path; leverage stays in pnl_pct only.
            pnl = (exit_price - entry_price) * size * signed

    return {
        "id": str(trade_row.get("id") or ""),
        "symbol": symbol,
        "side": direction,
        "entry_price": entry_price,
        "entry_time": str(trade_row.get("opened_at") or _now()),
        "exit_price": exit_price,
        "exit_time": exit_time,
        "size": size,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "strategy_name": strategy_name,
        "gross_pnl": pnl,
        "fees_paid": 0.0,
        "funding_pnl": 0.0,
        "net_pnl": pnl,
        "net_pnl_pct": pnl_pct,
        "entry_fee_bps": 0.0,
        "exit_fee_bps": 0.0,
        "close_reason": close_reason,
        "close_incomplete": close_incomplete,
    }


def _round_metric(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _build_session_performance_metrics(closed_trades: list[dict]) -> dict:
    pnl_values = [
        float(pnl)
        for trade in closed_trades
        if (pnl := trading_domain._coerce_optional_float(trade.get("pnl"))) is not None
    ]
    pnl_pct_values = [
        float(pnl_pct)
        for trade in closed_trades
        if (pnl_pct := trading_domain._coerce_optional_float(trade.get("pnl_pct"))) is not None
    ]
    closed_count = len(closed_trades)
    wins = [value for value in pnl_values if value > 0]
    losses = [value for value in pnl_values if value < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    net_pnl = sum(pnl_values)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss < 0 else (None if gross_profit <= 0 else gross_profit)
    win_rate_pct = (len(wins) / closed_count) * 100.0 if closed_count > 0 else 0.0
    avg_pnl = net_pnl / len(pnl_values) if pnl_values else 0.0
    avg_pnl_pct = sum(pnl_pct_values) / len(pnl_pct_values) if pnl_pct_values else 0.0
    last_trade_at = None
    if closed_trades:
        last_trade_at = str(closed_trades[0].get("exit_time") or closed_trades[0].get("entry_time") or "").strip() or None

    return {
        "closed_trades": closed_count,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "win_rate_pct": _round_metric(win_rate_pct, 4),
        "gross_profit": _round_metric(gross_profit, 4),
        "gross_loss": _round_metric(gross_loss, 4),
        "net_pnl": _round_metric(net_pnl, 4),
        "avg_pnl": _round_metric(avg_pnl, 4),
        "avg_pnl_pct": _round_metric(avg_pnl_pct, 4),
        "profit_factor": _round_metric(profit_factor, 4),
        "expectancy": _round_metric(avg_pnl, 4),
        "best_trade": _round_metric(max(pnl_values), 4) if pnl_values else None,
        "worst_trade": _round_metric(min(pnl_values), 4) if pnl_values else None,
        "last_trade_at": last_trade_at,
    }


_COMPAT_SESSION_PREFIX = "compat:strategy:"


def _compat_session_suffix(timestamp: object) -> str | None:
    raw = str(timestamp or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%Y%m%d%H%M%S")


def _compat_session_id(strategy_id: str, created_at: object | None = None) -> str:
    normalized_strategy_id = str(strategy_id or "").strip()
    suffix = _compat_session_suffix(created_at)
    if suffix:
        return f"{_COMPAT_SESSION_PREFIX}{normalized_strategy_id}:{suffix}"
    return f"{_COMPAT_SESSION_PREFIX}{normalized_strategy_id}"


def _compat_strategy_id_from_session_id(session_id: str) -> str:
    normalized = str(session_id or "").strip()
    if normalized.startswith(_COMPAT_SESSION_PREFIX):
        return normalized[len(_COMPAT_SESSION_PREFIX):].split(":", 1)[0]
    return normalized


def _trade_belongs_to_strategy_incarnation(trade_row: dict, strategy_row: dict) -> bool:
    cutoff = core._to_datetime_sort_key(strategy_row.get("created_at") or strategy_row.get("updated_at"))
    if cutoff <= 0:
        return True
    trade_started = core._to_datetime_sort_key(
        trade_row.get("opened_at") or trade_row.get("created_at") or trade_row.get("closed_at")
    )
    if trade_started <= 0:
        return True
    return trade_started >= cutoff


def _session_signal_snapshot(strategy_row: dict, scanner_signals: dict) -> dict:
    return _scanner_strategy_payload(strategy_row, scanner_signals)


def _scanner_strategy_payload(strategy_row: dict, payload_map: dict) -> dict:
    if not isinstance(payload_map, dict):
        return {}

    lookup: dict[str, dict] = {}
    for raw_key, raw_value in payload_map.items():
        if not isinstance(raw_value, dict):
            continue
        key = str(raw_key or "").strip().lower()
        if key:
            lookup[key] = raw_value

    for field in ("id", "display_id", "name"):
        candidate = str(strategy_row.get(field) or "").strip().lower()
        if not candidate:
            continue
        if candidate in lookup:
            return lookup[candidate]
    return {}


def _session_diagnostic_snapshot(strategy_row: dict, scanner_diagnostics: dict) -> dict:
    return _scanner_strategy_payload(strategy_row, scanner_diagnostics)


def _normalize_session_trade_mode(value: object) -> str | None:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return None
    if raw in {"both", "long_short", "long_and_short", "long/short", "bidirectional", "hedged"}:
        return "both"
    if raw in {"short", "short_only", "sell", "short_bias", "shorts"}:
        return "short_only"
    if raw in {"long", "long_only", "buy", "long_bias", "longs"}:
        return "long_only"
    return None


def _trade_mode_from_params(params: dict) -> str | None:
    for key in ("trade_mode", "position_mode", "position", "direction", "side", "bias"):
        if key not in params:
            continue
        mode = _normalize_session_trade_mode(params.get(key))
        if mode is not None:
            return mode
    return None


def _resolve_session_trade_mode(params: dict, position_sides: set[str]) -> str:
    configured = _trade_mode_from_params(params)
    if configured is not None:
        return configured
    clean_sides = {side for side in position_sides if side in {"long", "short"}}
    if {"long", "short"}.issubset(clean_sides):
        return "both"
    if clean_sides == {"short"}:
        return "short_only"
    return "long_only"


def _scoped_entry_exit(signal_snapshot: dict, position_sides: set | None) -> tuple[bool, bool]:
    """Direction-aware ``(entry_active, exit_active)`` for the dashboard.

    The snapshot's ``entry_signal``/``exit_signal`` are direction-AGNOSTIC (entry = any
    side's entry, exit = any side's exit), so a reversal bar — one that is simultaneously
    a SHORT entry AND a LONG exit — reads as "enter AND exit at once". When the snapshot
    carries the four directional flags, report entry/exit for the side that actually
    matters: the HELD position's side, else the side whose entry is firing. Falls back to
    the collapsed values for legacy snapshots without directional flags.
    """
    directional = signal_snapshot.get("directional_signals")
    if not isinstance(directional, dict):
        return bool(signal_snapshot.get("entry_signal")), bool(signal_snapshot.get("exit_signal"))
    sides = {str(s).strip().lower() for s in (position_sides or set())}
    sides = {s for s in sides if s in {"long", "short"}}
    side = None
    if len(sides) == 1:
        side = next(iter(sides))                                       # the held position's side
    elif bool(directional.get("short_entry")) and not bool(directional.get("long_entry")):
        side = "short"                                                 # flat, a short entry is firing
    elif bool(directional.get("long_entry")) and not bool(directional.get("short_entry")):
        side = "long"                                                  # flat, a long entry is firing
    if side:
        return bool(directional.get(f"{side}_entry")), bool(directional.get(f"{side}_exit"))
    # No position and no single-sided entry: surface any entry as informational, but there
    # is no position to exit, so don't raise a (cross-side) exit signal.
    return bool(directional.get("long_entry") or directional.get("short_entry")), False


def _build_session_runtime_fields(
    signal_snapshot: dict, timestamp: str, position_sides: set | None = None
) -> tuple[dict, list[dict], str]:
    entry_active, exit_active = _scoped_entry_exit(signal_snapshot, position_sides)

    indicators: dict[str, dict] = {}
    for name, value in signal_snapshot.items():
        # entry_signal/exit_signal are re-added below as direction-SCOPED values;
        # directional_signals is the nested carrier, not a chart indicator.
        if name in {"entry_signal", "exit_signal", "directional_signals"}:
            continue
        numeric = trading_domain._coerce_optional_float(value)
        if numeric is None:
            continue
        indicators[str(name)] = {
            "name": str(name),
            "value": numeric,
            "timestamp": timestamp,
        }
    if any(k in signal_snapshot for k in ("entry_signal", "exit_signal", "directional_signals")):
        indicators["entry_signal"] = {"name": "entry_signal", "value": 1.0 if entry_active else 0.0, "timestamp": timestamp}
        indicators["exit_signal"] = {"name": "exit_signal", "value": 1.0 if exit_active else 0.0, "timestamp": timestamp}

    pending_signals: list[dict] = []
    if entry_active:
        pending_signals.append(
            {
                "signal_type": "entry",
                "indicator_name": "entry_signal",
                "current_value": 1.0,
                "trigger_value": 1.0,
                "distance_pct": 0.0,
                "description": "Entry signal active",
            }
        )
    if exit_active:
        pending_signals.append(
            {
                "signal_type": "exit",
                "indicator_name": "exit_signal",
                "current_value": 1.0,
                "trigger_value": 1.0,
                "distance_pct": 0.0,
                "description": "Exit signal active",
            }
        )

    last_signal = "entry" if entry_active else ("exit" if exit_active else "none")
    return indicators, pending_signals, last_signal


def _resolve_real_account_snapshot(daemon_state: dict) -> dict:
    """Resolve the REAL Hyperliquid account balance the daemon caches each risk tick.

    The daemon persists the authoritative account equity (perp + spot, book-aware)
    into ``daemon_state['exchange_account']`` / ``daemon_state['account_equity']``
    every cycle, so the live "Capital" can show the true wallet WITHOUT any extra
    exchange round-trip from this (hot, list) endpoint — important because a
    synchronous exchange call here would risk starving the single-worker WebSocket.

    ``source`` distinguishes a genuine read ('exchange' master, 'books_only'
    books-sum, or 'books_aggregate' master+books) from the credentials-missing
    paper fallback ('paper'): only the former are real balances, so a
    paper/missing snapshot must surface as unavailable rather than silently
    re-introducing the fabricated $10k base.
    """
    exch = daemon_state.get("exchange_account") if isinstance(daemon_state, dict) else None
    exch = exch if isinstance(exch, dict) else {}
    source = str(exch.get("source") or "").strip().lower()
    account_value = trading_domain._coerce_optional_float(exch.get("accountValue"))
    if account_value is None and isinstance(daemon_state, dict):
        account_value = trading_domain._coerce_optional_float(daemon_state.get("account_equity"))
    available = bool(
        account_value is not None
        and account_value > 0
        and source in {"exchange", "books_only", "books_aggregate"}
    )
    return {
        "available": available,
        "account_value": account_value if available else None,
        "withdrawable": trading_domain._coerce_optional_float(exch.get("withdrawable")) if available else None,
        "margin_used": trading_domain._coerce_optional_float(exch.get("totalMarginUsed")) if available else None,
        "source": source if available else None,
        "network": (str(exch.get("network") or "").strip() or None) if available else None,
        "synced_at": (str(exch.get("synced_at") or "").strip() or None) if available else None,
    }


def _resolve_live_equity_baseline(strategy_id: str) -> float | None:
    """Stamped go-live account equity for a live strategy (its deploy-time cost
    basis), or None when no baseline was stamped (legacy/pre-stamp live rows)."""
    sid = str(strategy_id or "").strip()
    if not sid:
        return None
    try:
        stored = kv_get(live_equity_baseline_kv_key(sid), None)
    except Exception:
        return None
    value = trading_domain._coerce_optional_float(
        stored.get("equity") if isinstance(stored, dict) else stored
    )
    if value is not None and value > 0:
        return value
    return None


def _collect_compat_paper_sessions(
    include_deployed: bool = False,
    session_limit: int | None = None,
    trades_limit: int = 500,
) -> list[dict]:
    try:
        trades_cap = max(int(trades_limit), 1)
    except Exception:
        trades_cap = 500

    session_cap: int | None = None
    if session_limit is not None:
        try:
            parsed_session_limit = int(session_limit)
            if parsed_session_limit > 0:
                session_cap = parsed_session_limit
        except Exception:
            session_cap = None

    if include_deployed:
        status_filter_sql = (
            "LOWER(COALESCE(stage, status, '')) LIKE 'paper%' "
            "OR LOWER(COALESCE(stage, status, '')) LIKE 'live%' "
            "OR LOWER(COALESCE(stage, status, '')) LIKE 'deploy%' "
            "OR LOWER(COALESCE(status, '')) LIKE 'paper%' "
            "OR LOWER(COALESCE(status, '')) LIKE 'live%' "
            "OR LOWER(COALESCE(status, '')) LIKE 'deploy%'"
        )
    else:
        status_filter_sql = (
            "LOWER(COALESCE(stage, status, '')) LIKE 'paper%' "
            "OR LOWER(COALESCE(status, '')) LIKE 'paper%'"
        )

    with get_db() as conn:
        strategy_columns = {
            str(col["name"]).strip().lower()
            for col in conn.execute("PRAGMA table_info(strategies)").fetchall()
        }
        compat_column_sql = "compatible_regimes" if "compatible_regimes" in strategy_columns else "NULL AS compatible_regimes"
        rows = conn.execute(
            "SELECT id, display_id, name, type, symbol, timeframe, params, stage, status, created_at, updated_at, metrics, "
            f"{compat_column_sql} "
            f"FROM strategies WHERE {status_filter_sql} "
            "ORDER BY updated_at DESC, created_at DESC"
        ).fetchall()

    if not rows:
        return []

    daemon_state = kv_get("daemon_state", {}) or {}
    raw_prices = daemon_state.get("last_prices", {})
    price_map = raw_prices if isinstance(raw_prices, dict) else {}
    raw_exch_positions = daemon_state.get("exchange_positions", {})
    exchange_positions = raw_exch_positions if isinstance(raw_exch_positions, dict) else {}
    scanner_state = kv_get("scanner_state", {}) or {}
    scanner_signals = scanner_state.get("signals", {}) if isinstance(scanner_state, dict) else {}
    scanner_diagnostics = scanner_state.get("diagnostics", {}) if isinstance(scanner_state, dict) else {}
    scanner_ts = str(scanner_state.get("last_scan") or _now()) if isinstance(scanner_state, dict) else _now()
    recent_trades = trading_domain.read_recent_trades(limit=5000)

    # Real Hyperliquid balance for DEPLOYED/live sessions, read once from the cached
    # daemon snapshot (no per-session exchange round-trip — WS-starvation safe).
    real_account = _resolve_real_account_snapshot(daemon_state)

    # GO-LIVE-1 ceilings, read once — live opens above the ceiling are refused,
    # so the session card must show (and let the operator edit) the bound.
    live_ceilings: dict = {}
    if include_deployed:
        try:
            from forven.exchange.risk import get_live_notional_ceilings

            live_ceilings = get_live_notional_ceilings()
        except Exception:
            live_ceilings = {}

    sessions: list[dict] = []
    for row in rows:
        strategy_row = dict(row)
        strategy_id = str(strategy_row.get("id") or "").strip()
        if not strategy_id:
            continue

        strategy_name = (
            str(strategy_row.get("display_id") or "").strip()
            or str(strategy_row.get("name") or "").strip()
            or strategy_id
        )
        symbol = str(strategy_row.get("symbol") or "").strip().upper() or "BTC/USDT"
        timeframe = str(strategy_row.get("timeframe") or "").strip() or "1h"

        keys = _strategy_trade_keys(strategy_row)
        matched_trades = [
            trade
            for trade in recent_trades
            if _matches_strategy_trade(trade, keys)
            and _trade_belongs_to_strategy_incarnation(trade, strategy_row)
        ]

        open_trades = [
            trade
            for trade in matched_trades
            if str(trade.get("status") or "").strip().upper() == "OPEN"
        ]
        open_trades.sort(key=lambda trade: core._to_datetime_sort_key(trade.get("opened_at")), reverse=True)
        active_trade = open_trades[0] if open_trades else None

        closed_trades = [
            trade
            for trade in matched_trades
            if str(trade.get("status") or "").strip().upper() == "CLOSED"
        ]
        closed_trades.sort(
            key=lambda trade: core._to_datetime_sort_key(trade.get("closed_at") or trade.get("opened_at")),
            reverse=True,
        )

        all_closed_trade_views = [
            _build_compat_paper_trade(trade, strategy_name=strategy_name, symbol=symbol)
            for trade in closed_trades
        ]
        session_trades = all_closed_trade_views[:trades_cap]
        performance = _build_session_performance_metrics(all_closed_trade_views)

        total_closed_pnl = sum(
            trading_domain._coerce_optional_float(trade.get("pnl")) or 0.0
            for trade in all_closed_trade_views
            if trading_domain._coerce_optional_float(trade.get("pnl")) is not None
        )
        winning_trades = sum(
            1
            for trade in all_closed_trade_views
            if (trading_domain._coerce_optional_float(trade.get("pnl")) or 0.0) > 0
        )

        current_price = _resolve_session_current_price(price_map, symbol)
        if current_price is None and active_trade is not None:
            current_price = trading_domain._coerce_optional_float(active_trade.get("entry_price"))
        if current_price is None:
            current_price = 0.0

        positions: list[dict] = []
        unrealized_pnl = 0.0
        fallback_time = str((active_trade or {}).get("opened_at") or strategy_row.get("updated_at") or _now())
        for open_trade in open_trades:
            position_view, trade_unrealized = _build_session_position_view(
                open_trade,
                current_price=float(current_price),
                fallback_time=fallback_time,
                exchange_positions=exchange_positions,
            )
            if position_view is None:
                continue
            positions.append(position_view)
            unrealized_pnl += float(trade_unrealized or 0.0)

        position = positions[0] if len(positions) == 1 else None
        net_position = _build_net_position_view(positions, current_price=float(current_price))
        position_sides = {str(pos.get("side") or "").lower() for pos in positions}

        signal_snapshot = _session_signal_snapshot(strategy_row, scanner_signals)
        diagnostic_snapshot = _session_diagnostic_snapshot(strategy_row, scanner_diagnostics)
        diagnostic_blocked_reason = str(diagnostic_snapshot.get("blocked_reason") or "").strip()
        indicators, pending_signals, last_signal = _build_session_runtime_fields(
            signal_snapshot, scanner_ts, position_sides=position_sides
        )
        if "price" not in indicators and current_price > 0:
            indicators["price"] = {
                "name": "price",
                "value": current_price,
                "timestamp": scanner_ts,
            }

        params_dict = _parse_strategy_params(strategy_row.get("params"))
        diagnostic_params = diagnostic_snapshot.get("canonical_params") if isinstance(diagnostic_snapshot, dict) else None
        decision_params = dict(diagnostic_params) if isinstance(diagnostic_params, dict) and diagnostic_params else dict(params_dict)
        session_leverage = (
            trading_domain._coerce_optional_float((active_trade or {}).get("leverage"))
            or trading_domain._coerce_optional_float(decision_params.get("leverage"))
            or trading_domain._coerce_optional_float(params_dict.get("leverage"))
            or 1.0
        )

        stage_status = core._to_core_status(str(strategy_row.get("stage") or strategy_row.get("status") or "")) or ""
        is_deployed = stage_status == "live_graduated"
        total_pnl = total_closed_pnl + unrealized_pnl

        # Capital semantics differ by stage:
        #  - PAPER: an isolated $10k sandbox; capital = base + reconstructed PnL.
        #  - LIVE/deployed: the REAL Hyperliquid wallet equity. accountValue already
        #    embeds realized+unrealized PnL on-exchange, so we set capital to it
        #    DIRECTLY (adding total_pnl would double-count). The % return is anchored
        #    to the stamped go-live baseline (its true deploy basis), falling back to
        #    a derived cost basis (equity - this strategy's PnL) for legacy live rows
        #    with no stamp. When no real snapshot is available (daemon not yet synced
        #    / creds missing) we DO NOT present the $10k sandbox as if it were the
        #    live wallet — balance_source='unavailable' tells the UI to say so.
        account_value: float | None = None
        account_withdrawable: float | None = None
        account_margin_used: float | None = None
        account_network: str | None = None
        account_synced_at: str | None = None
        live_notional_ceiling_usd: float | None = None
        if is_deployed:
            ceiling_entry = live_ceilings.get(strategy_id)
            if isinstance(ceiling_entry, dict):
                ceiling_value = trading_domain._coerce_optional_float(ceiling_entry.get("ceiling_usd"))
                if ceiling_value and ceiling_value > 0:
                    live_notional_ceiling_usd = ceiling_value
            if real_account["available"]:
                account_value = float(real_account["account_value"])
                account_withdrawable = real_account["withdrawable"]
                account_margin_used = real_account["margin_used"]
                account_network = real_account["network"]
                account_synced_at = real_account["synced_at"]
                balance_source = real_account["source"]
                capital = account_value
                baseline = _resolve_live_equity_baseline(strategy_id)
                if baseline is None or baseline <= 0:
                    derived = account_value - total_pnl
                    baseline = derived if derived > 0 else account_value
                initial_capital = baseline
                total_pnl_pct = (total_pnl / baseline) * 100.0 if baseline > 0 else 0.0
            else:
                # Deployed but the real balance has not synced (daemon not yet ticked
                # / creds missing). Anchor NOTHING to the fabricated $10k sandbox base:
                # capital/initial_capital are unavailable and the % is undefined, so the
                # UI shows "balance unavailable" / "--" rather than a $10k-derived value.
                # total_pnl (this strategy's own realized/unrealized $) is still real.
                balance_source = "unavailable"
                initial_capital = None
                capital = None
                total_pnl_pct = None
        else:
            balance_source = "simulated"
            initial_capital = 10_000.0
            capital = initial_capital + total_pnl
            total_pnl_pct = (total_pnl / initial_capital) * 100.0 if initial_capital > 0 else 0.0
        session_trade_mode = _resolve_session_trade_mode(decision_params or params_dict, position_sides)
        session_position_model = "hedged" if session_trade_mode == "both" else "single_side"
        session_status = "position_open" if positions else _to_paper_session_status(
            strategy_row.get("stage"),
            strategy_row.get("status"),
        )

        started_at = (
            str((active_trade or {}).get("opened_at") or "").strip()
            or str(strategy_row.get("updated_at") or "").strip()
            or str(strategy_row.get("created_at") or "").strip()
            or None
        )

        gated_by_regime = False
        gated_reason = ""
        if diagnostic_blocked_reason:
            lowered_blocked_reason = diagnostic_blocked_reason.lower()
            if "regime" in lowered_blocked_reason:
                gated_by_regime = True
                gated_reason = diagnostic_blocked_reason
            if not positions:
                session_status = "gated" if gated_by_regime else "blocked"

        sessions.append(
            {
                "id": _compat_session_id(
                    strategy_id,
                    strategy_row.get("created_at") or strategy_row.get("updated_at"),
                ),
                "strategy_id": strategy_id,
                "strategy_name": strategy_name,
                "strategy_type": str(strategy_row.get("type") or "").strip() or None,
                "runtime_type": str(diagnostic_snapshot.get("runtime_type") or strategy_row.get("type") or "").strip() or None,
                "runtime_source": str(diagnostic_snapshot.get("runtime_source") or "").strip() or None,
                "strategy_version": "1.0.0",
                "symbol": symbol,
                "timeframe": timeframe,
                "params": params_dict,
                "default_params": params_dict,
                "decision_params": decision_params,
                "runtime_diagnostics": diagnostic_snapshot or None,
                "mode": "live",
                "live_feed": "default",
                "ibkr_sec_type": "STK",
                "ibkr_exchange": "SMART",
                "ibkr_currency": "USD",
                "ibkr_what_to_show": "TRADES",
                "replay_start": None,
                "replay_end": None,
                "replay_speed": 1,
                "initial_capital": initial_capital,
                "position_size_pct": 100.0,
                "stop_loss_pct": None,
                "take_profit_pct": None,
                "trailing_stop_pct": None,
                "fee_mode": "taker",
                "taker_fee_bps": 4.5,
                "maker_fee_bps": 1.5,
                "funding_mode": "off",
                "funding_rate_bps_per_interval": 0.0,
                "funding_interval_hours": 8,
                "leverage": session_leverage,
                "accrued_funding": 0.0,
                "status": session_status,
                "current_price": current_price,
                "position": position,
                "positions": positions,
                "net_position": net_position,
                "trade_mode": session_trade_mode,
                "position_model": session_position_model,
                "trades": session_trades,
                "indicators": indicators,
                "pending_signals": pending_signals,
                "last_signal": last_signal,
                "capital": capital,
                # Real Hyperliquid balance fields (deployed/live only). For paper
                # sessions account_value is None and balance_source='simulated';
                # the UI uses these to show the true wallet for live cards and a
                # 'balance unavailable' state instead of a fabricated number.
                "account_value": account_value,
                "account_withdrawable": account_withdrawable,
                "account_margin_used": account_margin_used,
                "balance_source": balance_source,
                "account_network": account_network,
                "account_synced_at": account_synced_at,
                # GO-LIVE-1: per-asset notional ceiling enforced on every live
                # open (None = no per-strategy cap; deployed sessions only).
                "live_notional_ceiling_usd": live_notional_ceiling_usd,
                "total_pnl": total_pnl,
                "total_pnl_pct": total_pnl_pct,
                "total_trades": len(all_closed_trade_views),
                "winning_trades": winning_trades,
                "performance": performance,
                "win_rate_pct": performance.get("win_rate_pct"),
                "avg_pnl": performance.get("avg_pnl"),
                "avg_pnl_pct": performance.get("avg_pnl_pct"),
                "profit_factor": performance.get("profit_factor"),
                "expectancy": performance.get("expectancy"),
                "started_at": started_at,
                "compat_kind": "deployed" if is_deployed else "paper",
                "gated_by_regime": gated_by_regime,
                "gated_reason": gated_reason,
                "blocked_reason": diagnostic_blocked_reason or None,
            }
        )

    sessions.sort(
        key=lambda session: core._to_datetime_sort_key(session.get("started_at") or _now()),
        reverse=True,
    )
    if session_cap is not None:
        return sessions[:session_cap]
    return sessions


def _find_compat_paper_session(session_id: str, include_deployed: bool = True) -> dict:
    target = str(session_id or "").strip()
    if not target:
        raise HTTPException(status_code=404, detail="paper session not found")

    normalized_target_id = _compat_session_id(_compat_strategy_id_from_session_id(target))
    target_strategy_id = _compat_strategy_id_from_session_id(target).strip()

    sessions = _collect_compat_paper_sessions(include_deployed=include_deployed)
    for session in sessions:
        session_id_value = str(session.get("id") or "").strip()
        strategy_id_value = _compat_strategy_id_from_session_id(session_id_value)
        if target == session_id_value:
            return session
        if target == strategy_id_value:
            return session
        if target.startswith(_COMPAT_SESSION_PREFIX) and target_strategy_id == strategy_id_value:
            return session
        if normalized_target_id == session_id_value and target_strategy_id == strategy_id_value:
            return session
    raise HTTPException(status_code=404, detail=f"paper session not found: {target}")


def _load_session_bars(
    session: dict,
    limit: int = 500,
    timeframe_override: str | None = None,
) -> list[dict]:
    requested = max(min(int(limit or 500), 2000), 50)
    symbol = str(session.get("symbol") or "").strip().upper()
    interval = (
        str(timeframe_override or session.get("timeframe") or "1h").strip().lower()
        or "1h"
    )
    asset = trading_domain._normalize_asset_key(symbol)
    if not asset:
        return []

    # Source-aware (Binance by default) so the chart shows the SAME real-exchange
    # prices the strategy trades on — not HyperLiquid testnet (which drifts).
    # include_unclosed=True keeps the live FORMING bar so the chart matches
    # TradingView/Binance (not one closed bar behind); signals still use closed bars.
    try:
        frame = fetch_market_candles(asset, bars=requested, interval=interval, include_unclosed=True)
    except Exception:
        if timeframe_override:
            return []
        try:
            frame = fetch_market_candles(asset, bars=requested, interval="1h", include_unclosed=True)
        except Exception:
            return []

    bars: list[dict] = []
    for timestamp, row in frame.tail(requested).iterrows():
        iso = trading_domain._coerce_iso_timestamp(getattr(timestamp, "isoformat", lambda: str(timestamp))())
        if not iso:
            continue
        bars.append(
            {
                "timestamp": iso,
                "open": float(row.get("open", 0.0)),
                "high": float(row.get("high", 0.0)),
                "low": float(row.get("low", 0.0)),
                "close": float(row.get("close", 0.0)),
                "volume": float(row.get("volume", 0.0)),
            }
        )
    return bars[-max(int(limit or 500), 1):]


def _ema_series(values: list[float], span: int) -> list[float | None]:
    if span <= 0:
        return [None for _ in values]
    alpha = 2.0 / (float(span) + 1.0)
    output: list[float | None] = []
    prev: float | None = None
    for value in values:
        prev = value if prev is None else (alpha * value + (1.0 - alpha) * prev)
        output.append(prev)
    return output


def _rsi_series(values: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None for _ in values]
    if period <= 0 or len(values) <= period:
        return output

    gains = [0.0 for _ in values]
    losses = [0.0 for _ in values]
    for idx in range(1, len(values)):
        delta = values[idx] - values[idx - 1]
        gains[idx] = max(delta, 0.0)
        losses[idx] = max(-delta, 0.0)

    for idx in range(period, len(values)):
        window_start = idx - period + 1
        avg_gain = sum(gains[window_start : idx + 1]) / float(period)
        avg_loss = sum(losses[window_start : idx + 1]) / float(period)
        if avg_loss <= 1e-9:
            output[idx] = 100.0
            continue
        rs = avg_gain / avg_loss
        output[idx] = 100.0 - (100.0 / (1.0 + rs))
    return output


def _rolling_sum(values: list[float], window: int) -> list[float | None]:
    output: list[float | None] = [None for _ in values]
    if window <= 0:
        return output
    running = 0.0
    for idx, value in enumerate(values):
        running += float(value)
        if idx >= window:
            running -= float(values[idx - window])
        if idx >= window - 1:
            output[idx] = running
    return output


def _rolling_mean(values: list[float], window: int) -> list[float | None]:
    sums = _rolling_sum(values, window)
    if window <= 0:
        return sums
    return [None if total is None else total / float(window) for total in sums]


def _atr_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None for _ in closes]
    if period <= 0 or not closes:
        return output

    true_ranges: list[float] = []
    for idx, close in enumerate(closes):
        high = highs[idx]
        low = lows[idx]
        if idx == 0:
            true_ranges.append(max(high - low, 0.0))
            continue
        prev_close = closes[idx - 1]
        true_ranges.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )
    return _rolling_mean(true_ranges, period)


def _macd_series(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None]]:
    fast_series = _ema_series(values, fast)
    slow_series = _ema_series(values, slow)
    macd_line: list[float | None] = []
    for fast_value, slow_value in zip(fast_series, slow_series):
        if fast_value is None or slow_value is None:
            macd_line.append(None)
            continue
        macd_line.append(fast_value - slow_value)

    signal_seed = [value if value is not None else 0.0 for value in macd_line]
    signal_line_raw = _ema_series(signal_seed, signal)
    signal_line = [
        None if macd_line[idx] is None else signal_line_raw[idx]
        for idx in range(len(macd_line))
    ]
    return macd_line, signal_line


def _adx_series(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    output: list[float | None] = [None for _ in closes]
    if period <= 0 or len(closes) <= period:
        return output

    plus_dm = [0.0 for _ in closes]
    minus_dm = [0.0 for _ in closes]
    true_ranges = [0.0 for _ in closes]
    for idx in range(1, len(closes)):
        up_move = highs[idx] - highs[idx - 1]
        down_move = lows[idx - 1] - lows[idx]
        plus_dm[idx] = up_move if up_move > down_move and up_move > 0 else 0.0
        minus_dm[idx] = down_move if down_move > up_move and down_move > 0 else 0.0
        true_ranges[idx] = max(
            highs[idx] - lows[idx],
            abs(highs[idx] - closes[idx - 1]),
            abs(lows[idx] - closes[idx - 1]),
        )

    tr_sum = _rolling_sum(true_ranges, period)
    plus_sum = _rolling_sum(plus_dm, period)
    minus_sum = _rolling_sum(minus_dm, period)

    dx: list[float | None] = [None for _ in closes]
    for idx in range(len(closes)):
        tr_value = tr_sum[idx]
        plus_value = plus_sum[idx]
        minus_value = minus_sum[idx]
        if tr_value is None or plus_value is None or minus_value is None or tr_value <= 1e-9:
            continue
        plus_di = 100.0 * plus_value / tr_value
        minus_di = 100.0 * minus_value / tr_value
        denominator = plus_di + minus_di
        dx[idx] = 0.0 if denominator <= 1e-9 else 100.0 * abs(plus_di - minus_di) / denominator

    for idx in range(len(dx)):
        if idx < (period * 2) - 2:
            continue
        window = dx[idx - period + 1 : idx + 1]
        if any(value is None for value in window):
            continue
        output[idx] = sum(float(value) for value in window if value is not None) / float(period)
    return output


def _series_to_history(timestamps: list[str], values: list[float | None]) -> list[dict]:
    return [
        {"timestamp": timestamp, "value": value}
        for timestamp, value in zip(timestamps, values)
        if timestamp
    ]


def _indicator_period_from_name(name: str, fallback: int) -> int:
    matches = re.findall(r"(\d+)", str(name or ""))
    if matches:
        try:
            period = int(matches[-1])
            if period > 0:
                return period
        except Exception:
            pass
    return fallback


def _normalize_param_key(key: object) -> str:
    return str(key or "").strip().lower().replace("-", "_").replace(".", "_").replace(" ", "_")


def _indicator_period_from_params(params: dict, aliases: tuple[str, ...], fallback: int) -> int:
    if not isinstance(params, dict):
        return fallback
    normalized_aliases = {_normalize_param_key(alias) for alias in aliases}
    for raw_key, raw_value in params.items():
        normalized_key = _normalize_param_key(raw_key)
        matched = normalized_key in normalized_aliases or any(
            normalized_key.endswith(f"_{alias}") for alias in normalized_aliases
        )
        if not matched:
            continue
        parsed = trading_domain._coerce_optional_float(raw_value)
        if parsed is None or parsed <= 0:
            continue
        return max(int(round(parsed)), 1)
    return fallback


def _has_numeric_param(params: dict, aliases: tuple[str, ...]) -> bool:
    sentinel = -1
    return _indicator_period_from_params(params, aliases, sentinel) != sentinel


def _default_indicator_names_from_params(runtime: dict, params: dict) -> list[str]:
    names: list[str] = [str(name) for name in runtime.keys()]
    names.extend(["price", "ema_fast", "ema_slow", "rsi"])
    if _has_numeric_param(params, ("atr_period", "atr_length")):
        names.append("atr")
    if _has_numeric_param(params, ("adx_period", "adx_length")):
        names.append("adx")
    if _has_numeric_param(params, ("macd_fast", "macd_slow", "macd_signal", "fast", "slow", "signal")):
        names.extend(["macd", "macd_signal"])
    return list(dict.fromkeys(name for name in names if str(name or "").strip()))


def _classify_session_indicator(name: str) -> str:
    lower = str(name or "").strip().lower()
    if lower in {"price", "close", "entry_signal", "exit_signal"}:
        return "none"
    if any(token in lower for token in ("rsi", "adx", "macd", "cci", "williams", "stoch", "mfi", "roc", "mom", "atr")):
        return "sub"
    if any(token in lower for token in ("signal", "uptrend", "downtrend", "trigger", "condition", "flag", "state")):
        return "none"
    if any(token in lower for token in ("ema", "sma", "wma", "hma", "vwma", "vwap", "bb", "bollinger", "donchian", "dc_", "keltner", "supertrend", "ichimoku", "sar")):
        return "main"
    return "none"


def _indicator_color(name: str) -> str:
    lower = str(name or "").strip().lower()
    explicit_colors = {
        "price": "#94a3b8",
        "close": "#94a3b8",
        "rsi": "#8b5cf6",
        "prev_rsi": "#a78bfa",
        "macd": "#38bdf8",
        "macd_signal": "#f59e0b",
        "adx": "#22d3ee",
        "atr": "#fb7185",
        "ema_fast": "#22c55e",
        "ema_slow": "#c084fc",
        "ema_regime": "#60a5fa",
        "entry_signal": "#22c55e",
        "exit_signal": "#ef4444",
    }
    if lower in explicit_colors:
        return explicit_colors[lower]
    if lower.startswith("atr"):
        return "#fb7185"
    if lower.startswith("rsi"):
        return "#8b5cf6"
    if lower.startswith("macd_signal"):
        return "#f59e0b"
    if lower.startswith("macd"):
        return "#38bdf8"
    if lower.startswith("adx"):
        return "#22d3ee"
    if "ema" in lower:
        palette = ["#22c55e", "#60a5fa", "#f59e0b", "#c084fc", "#f97316"]
    elif any(token in lower for token in ("rsi", "macd", "adx", "atr", "cci", "williams", "stoch", "mfi", "roc", "mom")):
        palette = ["#8b5cf6", "#38bdf8", "#f59e0b", "#22d3ee", "#fb7185", "#f97316"]
    else:
        palette = ["#e5e7eb", "#22c55e", "#60a5fa", "#f59e0b", "#c084fc", "#fb7185"]
    stable_idx = sum(ord(ch) for ch in lower) % len(palette)
    return palette[stable_idx]


def _derive_indicator_history(
    name: str,
    timestamps: list[str],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    params: dict | None = None,
) -> list[dict] | None:
    lower = str(name or "").strip().lower()
    params_dict = params if isinstance(params, dict) else {}
    if not timestamps or not closes:
        return []

    if lower in {"price", "close"}:
        return _series_to_history(timestamps, closes)

    if lower == "ema_fast":
        return _series_to_history(
            timestamps,
            _ema_series(closes, _indicator_period_from_params(params_dict, ("ema_fast", "fast", "fast_period", "fast_length"), 50)),
        )
    if lower == "ema_slow":
        return _series_to_history(
            timestamps,
            _ema_series(closes, _indicator_period_from_params(params_dict, ("ema_slow", "slow", "slow_period", "slow_length"), 200)),
        )
    if lower == "ema_regime":
        return _series_to_history(
            timestamps,
            _ema_series(closes, _indicator_period_from_params(params_dict, ("ema_regime", "regime_ema", "trend_ema", "filter_ema"), 200)),
        )
    if lower.startswith("ema"):
        return _series_to_history(timestamps, _ema_series(closes, _indicator_period_from_name(lower, 20)))
    if lower.startswith("sma"):
        return _series_to_history(timestamps, _rolling_mean(closes, _indicator_period_from_name(lower, 20)))

    if lower == "rsi":
        return _series_to_history(
            timestamps,
            _rsi_series(closes, _indicator_period_from_params(params_dict, ("rsi_period", "rsi_length", "rsi_window"), 14)),
        )
    if lower == "prev_rsi":
        rsi_values = _rsi_series(
            closes,
            _indicator_period_from_params(params_dict, ("rsi_period", "rsi_length", "rsi_window"), 14),
        )
        return _series_to_history(timestamps, [None, *rsi_values[:-1]])
    if "rsi" in lower:
        return _series_to_history(timestamps, _rsi_series(closes, _indicator_period_from_name(lower, 14)))

    if lower == "atr":
        return _series_to_history(
            timestamps,
            _atr_series(highs, lows, closes, _indicator_period_from_params(params_dict, ("atr_period", "atr_length"), 14)),
        )
    if "atr" in lower:
        return _series_to_history(timestamps, _atr_series(highs, lows, closes, _indicator_period_from_name(lower, 14)))

    if lower == "macd":
        macd_line, _ = _macd_series(
            closes,
            fast=_indicator_period_from_params(params_dict, ("macd_fast", "fast", "fast_period"), 12),
            slow=_indicator_period_from_params(params_dict, ("macd_slow", "slow", "slow_period"), 26),
            signal=_indicator_period_from_params(params_dict, ("macd_signal", "signal", "signal_period"), 9),
        )
        return _series_to_history(timestamps, macd_line)
    if lower == "macd_signal":
        _, signal_line = _macd_series(
            closes,
            fast=_indicator_period_from_params(params_dict, ("macd_fast", "fast", "fast_period"), 12),
            slow=_indicator_period_from_params(params_dict, ("macd_slow", "slow", "slow_period"), 26),
            signal=_indicator_period_from_params(params_dict, ("macd_signal", "signal", "signal_period"), 9),
        )
        return _series_to_history(timestamps, signal_line)

    if lower == "adx" or "adx" in lower:
        period = _indicator_period_from_params(params_dict, ("adx_period", "adx_length"), _indicator_period_from_name(lower, 14))
        return _series_to_history(timestamps, _adx_series(highs, lows, closes, period))

    return None


def get_paper_sessions(
    include_deployed: bool = False,
    only_deployed: bool = False,
    session_limit: int | None = None,
    trades_limit: int = 500,
):
    include_live = bool(include_deployed or only_deployed)
    sessions = _collect_compat_paper_sessions(
        include_deployed=include_live,
        session_limit=session_limit,
        trades_limit=trades_limit,
    )
    if not only_deployed:
        return sessions
    deployed_sessions = [
        session
        for session in sessions
        if str(session.get("compat_kind") or "").strip().lower() == "deployed"
    ]
    if session_limit is None:
        return deployed_sessions
    try:
        cap = int(session_limit)
    except Exception:
        return deployed_sessions
    if cap <= 0:
        return deployed_sessions
    return deployed_sessions[:cap]


def get_paper_session(session_id: str):
    return _find_compat_paper_session(session_id, include_deployed=True)


def get_paper_session_trades(session_id: str, limit: int = 50):
    session = _find_compat_paper_session(session_id, include_deployed=True)
    trades = session.get("trades", [])
    if not isinstance(trades, list):
        return []
    return trades[: max(int(limit), 1)]


_UNATTRIBUTED_CLOSE_REASON = "unspecified"

# Generous cap so the per-session close_reason breakdown covers every closed
# trade a session can realistically accumulate.
_SUMMARY_TRADES_LIMIT = 100_000


def _normalize_close_reason(value: object) -> str:
    return str(value or "").strip().lower() or _UNATTRIBUTED_CLOSE_REASON


def _summarize_paper_sessions(sessions: list[dict]) -> dict:
    """Aggregate per-session realized PnL / win-rate / close_reason counts.

    Pure function over compat session payloads (see
    ``_collect_compat_paper_sessions``) so it can be unit-tested without a DB.
    The close_reason breakdown is the trust signal: it separates strategy
    exits from reconciler/stale closes.
    """
    session_rows: list[dict] = []
    total_closed = 0
    total_open = 0
    total_realized = 0.0
    total_wins = 0
    total_close_reasons: dict[str, int] = {}

    for session in sessions:
        if not isinstance(session, dict):
            continue
        raw_trades = session.get("trades")
        trades = [trade for trade in raw_trades if isinstance(trade, dict)] if isinstance(raw_trades, list) else []
        raw_positions = session.get("positions")
        open_count = len(raw_positions) if isinstance(raw_positions, list) else 0

        close_reasons: dict[str, int] = {}
        realized = 0.0
        wins = 0
        for trade in trades:
            reason = _normalize_close_reason(trade.get("close_reason"))
            close_reasons[reason] = close_reasons.get(reason, 0) + 1
            total_close_reasons[reason] = total_close_reasons.get(reason, 0) + 1
            pnl = trading_domain._coerce_optional_float(trade.get("pnl"))
            if pnl is not None:
                realized += float(pnl)
                if pnl > 0:
                    wins += 1

        closed_count = len(trades)
        win_rate_pct = (wins / closed_count) * 100.0 if closed_count > 0 else None
        session_rows.append(
            {
                "session_id": str(session.get("id") or ""),
                "strategy_id": str(session.get("strategy_id") or ""),
                "strategy_name": str(session.get("strategy_name") or ""),
                "symbol": str(session.get("symbol") or ""),
                "timeframe": str(session.get("timeframe") or ""),
                "status": str(session.get("status") or ""),
                "closed_count": closed_count,
                "open_count": open_count,
                "realized_pnl_usd": _round_metric(realized, 4) if closed_count > 0 else 0.0,
                "win_rate_pct": _round_metric(win_rate_pct, 4),
                "close_reasons": dict(sorted(close_reasons.items(), key=lambda item: (-item[1], item[0]))),
            }
        )

        total_closed += closed_count
        total_open += open_count
        total_realized += realized
        total_wins += wins

    total_win_rate = (total_wins / total_closed) * 100.0 if total_closed > 0 else None
    return {
        "sessions": session_rows,
        "totals": {
            "session_count": len(session_rows),
            "closed_count": total_closed,
            "open_count": total_open,
            "realized_pnl_usd": _round_metric(total_realized, 4) if total_closed > 0 else 0.0,
            "win_rate_pct": _round_metric(total_win_rate, 4),
            "close_reasons": dict(sorted(total_close_reasons.items(), key=lambda item: (-item[1], item[0]))),
        },
    }


def get_paper_summary(include_deployed: bool = False) -> dict:
    """Per-session paper PnL rollup with a close_reason breakdown."""
    sessions = _collect_compat_paper_sessions(
        include_deployed=bool(include_deployed),
        trades_limit=_SUMMARY_TRADES_LIMIT,
    )
    summary = _summarize_paper_sessions(sessions)
    summary["include_deployed"] = bool(include_deployed)
    summary["timestamp"] = _now()
    return summary


def _coerce_signal_marker_price(signal: dict, bar: dict) -> float | None:
    price = trading_domain._coerce_optional_float(signal.get("price"))
    if price is not None and price > 0:
        return price
    for key in ("close", "open"):
        fallback = trading_domain._coerce_optional_float(bar.get(key))
        if fallback is not None and fallback > 0:
            return fallback
    return None


def _signal_marker_direction(signal_type: object, metrics: dict | None = None) -> str:
    metrics_dict = metrics if isinstance(metrics, dict) else {}
    candidate = str(metrics_dict.get("direction") or signal_type or "").strip().lower()
    if "short" in candidate:
        return "short"
    return "long"


def _parse_signal_metrics(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    raw = str(value or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


_SIGNAL_MARKER_FALLBACK_BAR_LIMIT = 160


def _coerce_marker_limit(limit: int | None, *, default: int = 500, cap: int = 1000) -> int:
    try:
        return max(min(int(limit or default), cap), 1)
    except Exception:
        return default


def _session_runtime_is_blocked(session: dict) -> bool:
    source = str(session.get("runtime_source") or "").strip().lower()
    if source == "blocked":
        return True
    diagnostics = session.get("runtime_diagnostics")
    if not isinstance(diagnostics, dict):
        return False
    if str(diagnostics.get("blocked_reason") or "").strip():
        return True
    return str(diagnostics.get("execution_decision") or "").strip().lower() == "blocked"


def _load_persisted_signal_markers(session: dict, *, limit: int = 500) -> tuple[list[dict], list[dict], list[dict], bool]:
    strategy_id = str(session.get("strategy_id") or _compat_strategy_id_from_session_id(str(session.get("id") or ""))).strip()
    if not strategy_id:
        return [], [], [], False

    cap = _coerce_marker_limit(limit)

    try:
        with get_db() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, signal_type, matched, executed, price, match_reason, block_reason, metrics_json
                FROM scanner_signal_results
                WHERE strategy_id = ?
                ORDER BY ts ASC, id ASC
                LIMIT ?
                """,
                (strategy_id, cap),
            ).fetchall()
    except Exception:
        return [], [], [], False

    if not rows:
        return [], [], [], False

    entries: list[dict] = []
    exits: list[dict] = []
    blocked: list[dict] = []
    active_keys: set[tuple[str, str]] = set()
    for row in rows:
        signal_type = str(row["signal_type"] or "").strip().lower()
        timestamp = str(row["ts"] or "").strip()
        if not timestamp:
            continue
        price = trading_domain._coerce_optional_float(row["price"])
        metrics = _parse_signal_metrics(row["metrics_json"])
        direction = _signal_marker_direction(signal_type, metrics)
        executed = bool(row["executed"])
        matched = bool(row["matched"])

        if matched and signal_type in {"entry", "exit"} and price is not None:
            key = (signal_type, direction)
            if key in active_keys:
                continue
            active_keys = {key}
            target = entries if signal_type == "entry" else exits
            target.append(
                {
                    "timestamp": timestamp,
                    "price": price,
                    "trade_id": f"signal:persisted:{signal_type}:{strategy_id}:{row['id']}",
                    "is_open": False,
                    "direction": direction,
                    "marker_kind": "signal",
                    "reason": str(row["match_reason"] or signal_type),
                    "executed": executed,
                }
            )
            continue

        active_keys = set()
        if not matched and price is not None:
            blocked.append(
                {
                    "timestamp": timestamp,
                    "price": price,
                    "trade_id": f"signal:persisted:blocked:{strategy_id}:{row['id']}",
                    "is_open": False,
                    "direction": direction,
                    "marker_kind": "blocked",
                    "reason": str(row["block_reason"] or "no_signal"),
                    "executed": executed,
                }
            )

    return entries, exits, blocked, True


def _build_strategy_signal_markers(session: dict, *, limit: int = 500) -> tuple[list[dict], list[dict]]:
    strategy_id = str(session.get("strategy_id") or _compat_strategy_id_from_session_id(str(session.get("id") or ""))).strip()
    strategy_type = str(session.get("runtime_type") or session.get("strategy_type") or "").strip()
    if not strategy_id or not strategy_type:
        return [], []

    bars = _load_session_bars(session, limit=max(min(int(limit or 500), 500), 50))
    if len(bars) < 2:
        return [], []

    try:
        import pandas as pd
        from forven.scanner import get_signal
    except Exception:
        return [], []

    params = session.get("decision_params") if isinstance(session.get("decision_params"), dict) else session.get("params")
    params_dict = dict(params) if isinstance(params, dict) else {}
    asset = trading_domain._normalize_asset_key(session.get("symbol")) or str(session.get("symbol") or "BTC").split("/", 1)[0]
    strategy_payload = {
        "asset": asset,
        "type": strategy_type,
        "runtime_type": strategy_type,
        "params": params_dict,
        "stage": "paper",
    }

    try:
        frame = pd.DataFrame(
            [
                {
                    "timestamp": bar.get("timestamp"),
                    "open": trading_domain._coerce_optional_float(bar.get("open")) or 0.0,
                    "high": trading_domain._coerce_optional_float(bar.get("high")) or 0.0,
                    "low": trading_domain._coerce_optional_float(bar.get("low")) or 0.0,
                    "close": trading_domain._coerce_optional_float(bar.get("close")) or 0.0,
                    "volume": trading_domain._coerce_optional_float(bar.get("volume")) or 0.0,
                }
                for bar in bars
            ]
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
        frame = frame.dropna(subset=["timestamp"]).set_index("timestamp")
    except Exception:
        return [], []

    entries: list[dict] = []
    exits: list[dict] = []
    active_keys: set[tuple[str, str]] = set()
    indexed_bars = list(bars)[-len(frame):]

    for idx in range(2, len(frame) + 1):
        try:
            signal = get_signal(strategy_id, strategy_payload, frame.iloc[:idx])
        except Exception:
            continue
        if not isinstance(signal, dict):
            continue

        bar = indexed_bars[idx - 1] if idx - 1 < len(indexed_bars) else {}
        timestamp = str(signal.get("bar_time") or signal.get("timestamp") or bar.get("timestamp") or "").strip()
        if not timestamp:
            continue
        price = _coerce_signal_marker_price(signal, bar)
        if price is None:
            continue

        direction = str(signal.get("direction") or "long").strip().lower() or "long"
        current_keys: set[tuple[str, str]] = set()

        if bool(signal.get("entry_signal")):
            key = ("entry", direction)
            current_keys.add(key)
            if key not in active_keys:
                entries.append(
                    {
                        "timestamp": timestamp,
                        "price": price,
                        "trade_id": f"signal:entry:{strategy_id}:{idx - 1}",
                        "is_open": False,
                        "direction": direction,
                        "marker_kind": "signal",
                        "reason": str(signal.get("match_reason") or "entry_signal"),
                    }
                )

        if bool(signal.get("exit_signal")):
            key = ("exit", direction)
            current_keys.add(key)
            if key not in active_keys:
                exits.append(
                    {
                        "timestamp": timestamp,
                        "price": price,
                        "trade_id": f"signal:exit:{strategy_id}:{idx - 1}",
                        "is_open": False,
                        "direction": direction,
                        "marker_kind": "signal",
                        "reason": str(signal.get("match_reason") or "exit_signal"),
                    }
                )

        active_keys = current_keys

    return entries, exits


# Self-describing marker visuals (industry-standard trading-chart conventions).
# Real fills are four DISTINCT labeled markers; would-be triggers are smaller,
# muted arrows. Every marker carries shape/color/side/action so the frontend can
# render straight from the payload (it still falls back to direction if absent).
_MARK_BUY = "#22c55e"      # long entry  (BUY)
_MARK_SELL = "#ef4444"     # long exit   (SELL)
_MARK_SHORT = "#f97316"    # short entry (SHORT, orange)
_MARK_COVER = "#14b8a6"    # short exit  (COVER, teal)
_MARK_TRIG_ENTRY = "#4ade80"  # muted green: would-be entry trigger
_MARK_TRIG_EXIT = "#f87171"   # muted red:   would-be exit trigger


def _trade_marker_fields(direction: str, leg: str) -> dict:
    """Self-describing visual fields for a REAL fill. ``leg`` is 'entry' or 'exit'.

    side ('bull'|'bear') drives above/below-bar placement on the frontend;
    BUY/COVER sit below the bar (bullish), SELL/SHORT sit above (bearish)."""
    is_short = str(direction or "long").strip().lower() == "short"
    if leg == "entry":
        if is_short:
            return {"side": "bear", "action": "short", "shape": "arrowDown", "color": _MARK_SHORT, "label": "SHORT"}
        return {"side": "bull", "action": "buy", "shape": "arrowUp", "color": _MARK_BUY, "label": "BUY"}
    if is_short:
        return {"side": "bull", "action": "cover", "shape": "arrowUp", "color": _MARK_COVER, "label": "COVER"}
    return {"side": "bear", "action": "sell", "shape": "arrowDown", "color": _MARK_SELL, "label": "SELL"}


def _trigger_marker_fields(direction: str, leg: str) -> dict:
    """Self-describing visual fields for a would-be open/close TRIGGER, using the
    BUY / SELL / SHORT / COVER convention so the full-history overlay reads like an
    order log. ``leg`` is 'entry'/'exit'.

    Buy-side actions are GREEN up-triangles below the bar; sell-side actions are RED
    down-triangles above the bar:
      * long entry  → BUY   (buy-side, green ▲)
      * short exit  → COVER (buy-side, green ▲)
      * long exit   → SELL  (sell-side, red ▼)
      * short entry → SHORT (sell-side, red ▼)
    """
    is_short = str(direction or "long").strip().lower() == "short"
    if leg == "entry":
        if is_short:  # SHORT — sell-side
            return {"side": "bear", "action": "short", "shape": "arrowDown", "color": _MARK_TRIG_EXIT}
        return {"side": "bull", "action": "buy", "shape": "arrowUp", "color": _MARK_TRIG_ENTRY}
    # exit
    if is_short:  # COVER — buy-side
        return {"side": "bull", "action": "cover", "shape": "arrowUp", "color": _MARK_TRIG_ENTRY}
    return {"side": "bear", "action": "sell", "shape": "arrowDown", "color": _MARK_TRIG_EXIT}


def get_paper_session_markers(
    session_id: str,
    *,
    limit: int = 500,
    include_generated: bool = False,
):
    session = _find_compat_paper_session(session_id, include_deployed=True)
    entries: list[dict] = []
    exits: list[dict] = []
    blocked: list[dict] = []
    marker_limit = _coerce_marker_limit(limit)

    for trade in session.get("trades", []) if isinstance(session.get("trades"), list) else []:
        trade_id = str(trade.get("id") or "")
        entry_ts = str(trade.get("entry_time") or "").strip()
        exit_ts = str(trade.get("exit_time") or "").strip()
        entry_price = trading_domain._coerce_optional_float(trade.get("entry_price"))
        exit_price = trading_domain._coerce_optional_float(trade.get("exit_price"))
        pnl = trading_domain._coerce_optional_float(trade.get("pnl"))
        pnl_pct = trading_domain._coerce_optional_float(trade.get("pnl_pct"))
        direction = str(trade.get("side") or "long").strip().lower()
        if entry_ts and entry_price is not None:
            entries.append(
                {
                    "timestamp": entry_ts,
                    "price": entry_price,
                    "trade_id": trade_id,
                    "is_open": False,
                    "direction": direction,
                    "marker_kind": "trade",
                    **_trade_marker_fields(direction, "entry"),
                }
            )
        if exit_ts and exit_price is not None:
            exits.append(
                {
                    "timestamp": exit_ts,
                    "price": exit_price,
                    "trade_id": trade_id,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "is_open": False,
                    "direction": direction,
                    "marker_kind": "trade",
                    **_trade_marker_fields(direction, "exit"),
                }
            )

    open_positions = session.get("positions") if isinstance(session.get("positions"), list) else []
    if not open_positions:
        position = session.get("position") if isinstance(session.get("position"), dict) else None
        if position:
            open_positions = [position]
    for index, position in enumerate(open_positions):
        entry_time = str((position or {}).get("entry_time") or "").strip()
        entry_price = trading_domain._coerce_optional_float((position or {}).get("entry_price"))
        if entry_time and entry_price is not None:
            _open_dir = str((position or {}).get("side") or "long").lower()
            entries.append(
                {
                    "timestamp": entry_time,
                    "price": entry_price,
                    "trade_id": f"open:{session.get('id')}:{index}",
                    "is_open": True,
                    "direction": _open_dir,
                    "marker_kind": "trade",
                    **_trade_marker_fields(_open_dir, "entry"),
                }
            )

    signal_entries, signal_exits, blocked, has_persisted_signals = _load_persisted_signal_markers(
        session,
        limit=marker_limit,
    )
    if include_generated and not has_persisted_signals and not _session_runtime_is_blocked(session):
        fallback_limit = min(marker_limit, _SIGNAL_MARKER_FALLBACK_BAR_LIMIT)
        signal_entries, signal_exits = _build_strategy_signal_markers(session, limit=fallback_limit)
    entries.extend(signal_entries)
    exits.extend(signal_exits)

    entries.sort(key=lambda row: core._to_datetime_sort_key(row.get("timestamp")))
    exits.sort(key=lambda row: core._to_datetime_sort_key(row.get("timestamp")))
    blocked.sort(key=lambda row: core._to_datetime_sort_key(row.get("timestamp")))
    return {"entries": entries, "exits": exits, "blocked": blocked}


# ─────────────────────────────────────────────────────────────────────────────
# Chart bundle (paper↔backtest parity overhaul, Phase 4)
#
# ONE endpoint that returns everything the trading chart needs, driven by the
# REAL indicator registry + the strategy's own signal function — so the chart
# shows exactly what the strategy trades on (no guessed reimplementation):
#   - main/sub indicator overlays from forven.strategies.indicators (the registry)
#   - trigger markers over the FULL history (every generate_signals entry/exit,
#     including bars from BEFORE the strategy went live)
#   - trade markers for every ACTUAL recorded paper trade
#   - the active stop / take-profit (and trailing) levels of the open position
# ─────────────────────────────────────────────────────────────────────────────


def _chart_indicator_specs(strategy_type: str, params: dict) -> list[dict]:
    """Resolve a strategy's REAL chart indicators to registry specs {id,kind,params}.

    rule_engine carries declared specs in params['indicators']; the builtins are
    mapped to the registry kinds they actually compute (param names translated to
    registry keys). Strategies with no resolvable specs return [] — the chart shows
    bars + triggers + trades with NO overlay (never a guessed reimplementation)."""
    p = params if isinstance(params, dict) else {}
    stype = str(strategy_type or "").strip().lower()

    if stype in ("rule_engine", "rule") or isinstance(p.get("indicators"), list):
        specs = p.get("indicators")
        return [s for s in specs if isinstance(s, dict) and s.get("kind")] if isinstance(specs, list) else []

    def _i(key, default):
        try:
            return int(p.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    def _f(key, default):
        try:
            return float(p.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    builders: dict[str, list[dict]] = {
        "rsi_momentum": [
            {"id": "rsi", "kind": "rsi", "params": {"length": _i("rsi_period", 14)}},
            {"id": "ema_fast", "kind": "ema", "params": {"length": _i("ema_fast", 50)}},
            {"id": "ema_slow", "kind": "ema", "params": {"length": _i("ema_slow", 200)}},
        ],
        "ema_cross": [
            {"id": "ema_fast", "kind": "ema", "params": {"length": _i("ema_fast", 20)}},
            {"id": "ema_slow", "kind": "ema", "params": {"length": _i("ema_slow", 50)}},
            {"id": "ema_regime", "kind": "ema", "params": {"length": _i("ema_regime", 200)}},
        ],
        "bollinger": [
            {"id": "bb", "kind": "bollinger", "params": {"length": _i("bb_period", 20), "num_std": _f("bb_std", 2.0)}},
            {"id": "rsi", "kind": "rsi", "params": {"length": _i("rsi_period", 14)}},
        ],
        "keltner": [{"id": "kc", "kind": "keltner", "params": {"length": _i("kc_period", 20), "atr_length": _i("kc_period", 20), "mult": _f("kc_mult", 2.0)}}],
        "macd": [{"id": "macd", "kind": "macd", "params": {"fast": _i("fast", 12), "slow": _i("slow", 26), "signal": _i("signal", 9)}}],
        "supertrend": [{"id": "st", "kind": "supertrend", "params": {"length": _i("atr_period", 10), "mult": _f("multiplier", 3.0)}}],
        "stochastic": [{"id": "stoch", "kind": "stochastic", "params": {"k": _i("k", 14), "d": _i("d", 3), "smooth": _i("smooth", 3)}}],
        "donchian": [{"id": "dc", "kind": "donchian", "params": {"length": _i("donchian_period", 20)}}],
        "parabolic_sar": [{"id": "psar", "kind": "psar", "params": {"step": _f("step", 0.02), "max_step": _f("max_step", 0.2)}}],
        "williams_r": [{"id": "wr", "kind": "williams_r", "params": {"length": _i("wr_period", _i("period", 14))}}],
        "ichimoku": [{"id": "ich", "kind": "ichimoku", "params": {"conversion": _i("tenkan_period", 9), "base": _i("kijun_period", 26), "span_b": _i("senkou_b_period", 52)}}],
        "funding": [{"id": "fz", "kind": "funding_zscore", "params": {"length": _i("zscore_period", 96)}}],
        "vwap": [{"id": "vwap", "kind": "vwap", "params": {"length": _i("vwap_period", 0)}}],
        # Mean-reversion variant of bollinger → same overlays (bands + RSI).
        "bollinger_reversion": [
            {"id": "bb", "kind": "bollinger", "params": {"length": _i("bb_period", 20), "num_std": _f("bb_std", 2.0)}},
            {"id": "rsi", "kind": "rsi", "params": {"length": _i("rsi_period", 14)}},
        ],
        # Composite types (forven/strategies/composite) mapped to the registry
        # indicators they actually gate on.
        "bb_rsi_reversion": [
            {"id": "bb", "kind": "bollinger", "params": {"length": _i("bb_period", 20), "num_std": _f("bb_std", 2.0)}},
            {"id": "rsi", "kind": "rsi", "params": {"length": _i("rsi_period", 14)}},
        ],
        "funding_fade_rsi": [
            {"id": "fz", "kind": "funding_zscore", "params": {"length": _i("funding_period", 48)}},
            {"id": "rsi", "kind": "rsi", "params": {"length": _i("rsi_period", 14)}},
        ],
        "macd_volume": [
            {"id": "macd", "kind": "macd", "params": {"fast": _i("fast", 12), "slow": _i("slow", 26), "signal": _i("signal", 9)}},
            {"id": "vol_sma", "kind": "volume_sma", "params": {"length": _i("vol_period", 20)}},
        ],
        "trend_keltner": [
            {"id": "kc", "kind": "keltner", "params": {"length": _i("kc_period", 20), "atr_length": _i("kc_period", 20), "mult": _f("kc_mult", 2.0)}},
            {"id": "ema_trend", "kind": "ema", "params": {"length": _i("ma_period", 100)}},
        ],
        # Opening-range breakout: the rolling high/low band the strategy breaks out
        # of == a donchian channel over the opening-range window.
        "orb": [{"id": "orb", "kind": "donchian", "params": {"length": _i("range_bars", 4)}}],
    }
    return builders.get(stype, [])


def _bars_to_frame(bars: list[dict]):
    import pandas as pd

    if not bars:
        return None
    idx = pd.to_datetime([b.get("timestamp") for b in bars], utc=True, errors="coerce")
    frame = pd.DataFrame(
        {
            "open": [trading_domain._coerce_optional_float(b.get("open")) or 0.0 for b in bars],
            "high": [trading_domain._coerce_optional_float(b.get("high")) or 0.0 for b in bars],
            "low": [trading_domain._coerce_optional_float(b.get("low")) or 0.0 for b in bars],
            "close": [trading_domain._coerce_optional_float(b.get("close")) or 0.0 for b in bars],
            "volume": [trading_domain._coerce_optional_float(b.get("volume")) or 0.0 for b in bars],
        },
        index=idx,
    )
    return frame[frame.index.notna()]


def _compute_chart_indicators(frame, specs: list[dict]) -> tuple[list[dict], list[dict], list[str]]:
    """Compute each spec via the registry into chart line series, split by panel."""
    import pandas as pd
    from forven.strategies import indicators as _ind

    main: list[dict] = []
    sub: list[dict] = []
    warnings: list[str] = []
    ts = [t.isoformat() for t in frame.index]
    for spec in specs:
        kind = str(spec.get("kind") or "").strip().lower()
        if not kind:
            continue
        panel = _ind.default_panel(kind)
        try:
            outputs = _ind.compute_indicator(frame, spec)
        except Exception as exc:  # one bad indicator must not break the chart
            warnings.append(f"indicator '{kind}' failed: {exc}")
            continue
        for name, series in outputs.items():
            vals = [
                {"timestamp": t, "value": (float(v) if (v is not None and pd.notna(v)) else None)}
                for t, v in zip(ts, list(series))
            ]
            cfg = {"name": name, "panel": panel, "type": "line", "color": _indicator_color(name), "data": vals}
            (main if panel == "main" else sub).append(cfg)
    return main, sub, warnings


def _resolve_chart_strategy(session: dict, params: dict):
    strat_id = str(session.get("strategy_id") or session.get("id") or "")
    asset = trading_domain._normalize_asset_key(str(session.get("symbol") or ""))
    try:
        from forven.strategies.registry import _TYPE_MAP, get_active, resolve_runtime_type

        inst = get_active().get(strat_id)
        if inst is not None:
            return inst
        runtime_type, _meta = resolve_runtime_type(str(session.get("type") or ""), session.get("runtime_type"))
        cls = _TYPE_MAP.get(runtime_type or "")
        if cls is not None:
            cp = dict(params)
            if asset:
                cp.setdefault("_asset", asset)
            return cls(strat_id, cp)
    except Exception:
        return None
    return None


def _resolve_trigger_trade_mode(strat, params: dict) -> str:
    """The trade mode the chart's trigger replay must run under. CRITICAL: without
    this it defaults to ``long_only`` and a SHORT-only strategy produces ZERO trades
    → ZERO triggers (the 'no triangles on the chart' bug). Mirrors the scanner's
    ``_resolve_kernel_trade_mode`` so the chart's would-be trades match paper's."""
    tm = str((params or {}).get("trade_mode") or "").strip().lower()
    if tm in ("long_only", "short_only", "both"):
        return tm
    modes = getattr(strat, "supported_trade_modes", None)
    if modes and "both" in modes:
        return "both"
    if modes and set(modes) == {"short_only"}:
        return "short_only"
    return "long_only"


def _kernel_trigger_markers(strat, frame, *, params, leverage, strategy_type, cutoff):
    """Discrete historical trigger points = the entries/exits the KERNEL would make
    over the whole frame (ungated). These are EVENTS (one per would-be position
    open/close), not raw per-bar signal states — so a strategy whose exit condition
    holds for long stretches doesn't light up every candle. Each is tagged
    buy/sell/short/cover (green/red triangle) via ``_trigger_marker_fields``.

    ``cutoff`` (an ISO timestamp) bounds which triggers are emitted to strictly
    BEFORE it; ``None`` (the chart's default) emits the FULL would-be history so the
    operator can compare the strategy's open/close logic against the actual trades."""
    import pandas as pd
    from forven.strategies import backtest as _bt

    entries: list[dict] = []
    exits: list[dict] = []
    if strat is None:
        return entries, exits
    try:
        # Use the strategy's FROZEN execution profile (stops/TP/trailing/time-stop), the
        # same one the live paper scan runs, so the chart's trigger triangles reflect the
        # actual entry/exit logic — a stop/time-stop exit shows where it really fires.
        # execution_controls=None drew signal-only triggers that diverged from real trades.
        _trigger_ec = _bt.execution_controls_from_params(params) or None
        res = _bt.run_strategy_execution(
            frame, strat, params=params, warmup=200, leverage=leverage,
            regime_gate=False, trade_mode=_resolve_trigger_trade_mode(strat, params),
            execution_controls=_trigger_ec, strategy_type=strategy_type,
        )
    except Exception:
        return entries, exits
    if res is None:  # no vectorized signals (per-bar-only strategy) → no triggers
        return entries, exits

    def _before(ts) -> bool:
        if cutoff is None:
            return True
        t = pd.to_datetime(ts, utc=True, errors="coerce")
        return t is not None and not pd.isna(t) and t < cutoff

    for t in res.closed_trades:
        d = str(t.get("direction") or "long")
        if t.get("entry_time") and _before(t["entry_time"]):
            entries.append({"timestamp": str(t["entry_time"]), "price": float(t["entry_price"]), "direction": d, "marker_kind": "signal", **_trigger_marker_fields(d, "entry")})
        if t.get("exit_time") and _before(t["exit_time"]):
            exits.append({"timestamp": str(t["exit_time"]), "price": float(t["exit_price"]), "direction": d, "marker_kind": "signal", **_trigger_marker_fields(d, "exit")})
    for d, pos in (res.open_positions or {}).items():
        if pos.get("entry_time") and _before(pos["entry_time"]):
            entries.append({"timestamp": str(pos["entry_time"]), "price": float(pos["entry_price"]), "direction": str(d), "marker_kind": "signal", **_trigger_marker_fields(str(d), "entry")})
    return entries, exits


def get_paper_session_chart(
    session_id: str,
    limit: int = 2000,
    timeframe: str | None = None,
):
    """The single chart bundle: bars + real indicators + full-history triggers +
    actual trade markers + the open position's active stop/take-profit/trailing.

    Read-only and REST-only (kept off the WS loop to avoid single-worker starvation)."""
    session = _find_compat_paper_session(session_id, include_deployed=True)
    params = session.get("decision_params") if isinstance(session.get("decision_params"), dict) else session.get("params")
    params = params if isinstance(params, dict) else {}
    strategy_type = str(session.get("runtime_type") or session.get("type") or "").strip()

    bars = _load_session_bars(session, limit=max(min(int(limit or 2000), 2000), 100), timeframe_override=timeframe)
    frame = _bars_to_frame(bars)


    main_indicators: list[dict] = []
    sub_indicators: list[dict] = []
    trigger_entries: list[dict] = []
    trigger_exits: list[dict] = []
    warnings: list[str] = []

    # Actual recorded trades → trade markers (reuse the existing marker builder's trade legs).
    marker_bundle = get_paper_session_markers(session_id, limit=limit, include_generated=False)
    entry_markers = [m for m in marker_bundle.get("entries", []) if m.get("marker_kind") == "trade"]
    exit_markers = [m for m in marker_bundle.get("exits", []) if m.get("marker_kind") == "trade"]

    leverage = float(params.get("leverage", 1.0) or 1.0)

    if frame is not None and len(frame) >= 2:
        specs = _chart_indicator_specs(strategy_type, params)
        if specs:
            main_indicators, sub_indicators, warnings = _compute_chart_indicators(frame, specs)
        else:
            warnings.append(f"No indicator overlay available for strategy type '{strategy_type or 'unknown'}'.")
        strat = _resolve_chart_strategy(session, params)
        # Full-history triggers: EVERY would-be open/close (buy/sell/short/cover) the
        # strategy makes across the whole chart, as green/red triangles — so the
        # operator can compare the strategy's signals against the actual trades.
        trigger_entries, trigger_exits = _kernel_trigger_markers(
            strat, frame, params=params, leverage=leverage, strategy_type=strategy_type, cutoff=None,
        )

    # Active levels from the open position. Every level is self-describing
    # (type/label/color/from_time/to_time) so the frontend draws the industry-
    # standard active-order lines straight from the payload. ``entry`` carries the
    # open position's entry price (blue solid "ENTRY" line for the whole hold).
    position = session.get("position") if isinstance(session.get("position"), dict) else None
    active_levels: dict[str, list[dict]] = {"stop": [], "take_profit": [], "trail": [], "entry": []}
    if position:
        side = str(position.get("side") or "long").strip().lower()
        entry_time = str(position.get("entry_time") or "")

        def _level(price: float, ltype: str, label: str, color: str) -> dict:
            # from_time anchors the line to the entry; to_time=None ⇒ still open.
            # (A full-width dashed price line + axis label is the TradingView-standard
            # representation of an active order. For a TRUE entry-anchored ray, draw a
            # 2-point line series instead — see the trendLine pattern in
            # ChartWorkspace.svelte: chart.addLineSeries(...).setData([{start},{now}]).)
            return {"price": price, "direction": side, "from_time": entry_time, "to_time": None,
                    "type": ltype, "label": label, "color": color}

        entry_price = trading_domain._coerce_optional_float(position.get("entry_price"))
        if entry_price is not None:
            active_levels["entry"].append(_level(entry_price, "entry", "ENTRY", "#3b82f6"))
        sl = trading_domain._coerce_optional_float(position.get("stop_loss_price"))
        tp = trading_domain._coerce_optional_float(position.get("take_profit_price"))
        if sl is not None:
            active_levels["stop"].append(_level(sl, "stop", "SL", "#ef4444"))
        if tp is not None:
            active_levels["take_profit"].append(_level(tp, "take_profit", "TP", "#22c55e"))
        trail = trading_domain._coerce_optional_float(
            parse_trade_signal_data(position.get("signal_data")).get("trailing_stop_price")
        )
        if trail is not None:
            active_levels["trail"].append(_level(trail, "trail", "Trail", "#f59e0b"))

    return {
        "session_id": str(session.get("id") or session_id),
        "bars": bars,
        "main_indicators": main_indicators,
        "sub_indicators": sub_indicators,
        "entry_markers": entry_markers,
        "exit_markers": exit_markers,
        "trigger_entries": trigger_entries,
        "trigger_exits": trigger_exits,
        "active_levels": active_levels,
        "strategy_type": strategy_type,
        "warnings": warnings,
    }


def get_paper_session_indicators(
    session_id: str,
    indicators: str | None = None,
    limit: int = 500,
    timeframe: str | None = None,
):
    session = _find_compat_paper_session(session_id, include_deployed=True)
    runtime = session.get("indicators", {}) if isinstance(session.get("indicators"), dict) else {}
    params = session.get("decision_params") if isinstance(session.get("decision_params"), dict) else session.get("params")
    params_dict = params if isinstance(params, dict) else {}
    runtime_by_name: dict[str, dict] = {}
    for key, value in runtime.items():
        if isinstance(value, dict):
            runtime_by_name[str(key).strip().lower()] = value

    requested_names = [part.strip() for part in str(indicators or "").split(",") if part.strip()]
    default_names = _default_indicator_names_from_params(runtime, params_dict)
    names = requested_names or list(dict.fromkeys(default_names))

    bars = _load_session_bars(
        session,
        limit=max(min(int(limit), 1000), 100),
        timeframe_override=timeframe,
    )
    timestamps = [str(bar.get("timestamp") or "") for bar in bars]
    highs = [trading_domain._coerce_optional_float(bar.get("high")) or 0.0 for bar in bars]
    lows = [trading_domain._coerce_optional_float(bar.get("low")) or 0.0 for bar in bars]
    closes = [trading_domain._coerce_optional_float(bar.get("close")) for bar in bars]
    close_values = [value if value is not None else 0.0 for value in closes]

    config: dict[str, dict] = {}
    history_payload: dict[str, list[dict]] = {}
    for name in names:
        lower = str(name).strip().lower()
        panel = _classify_session_indicator(name)
        config[name] = {"panel": panel, "type": "line", "color": _indicator_color(name)}

        derived = _derive_indicator_history(name, timestamps, highs, lows, close_values, params=params_dict)
        if derived:
            history_payload[name] = derived[-max(int(limit), 1):]
            continue

        row = runtime_by_name.get(lower)
        if row:
            row_ts = str(row.get("timestamp") or session.get("started_at") or _now())
            row_value = trading_domain._coerce_optional_float(row.get("value"))
            history_payload[name] = [{"timestamp": row_ts, "value": row_value}]
            continue

        history_payload[name] = []

    return {
        "session_id": str(session.get("id") or session_id),
        "config": config,
        "indicators": history_payload,
    }


def get_paper_session_replay_bars(
    session_id: str,
    limit: int = 500,
    timeframe: str | None = None,
):
    session = _find_compat_paper_session(session_id, include_deployed=True)
    return _load_session_bars(session, limit=limit, timeframe_override=timeframe)


_PAPER_SERVICE_TEST_BACKUP_KEY = "paper_service:test_mode_backup"
_PAPER_TEST_MODE_TTL = timedelta(hours=2)
_PAPER_TEST_SETTING_KEYS = (
    "throughput_auto_scheduler_control",
    "relaxed_trade_filters_enabled",
    "strict_regime_gating",
    "allow_unknown_regime_strategies",
    "scanner_signal_interval_minutes",
    "scanner_execution_interval_minutes",
    "paper_test_mode_enabled",
    "paper_test_high_activity_enabled",
    "paper_test_bypass_gates_enabled",
    "paper_test_local_execution_only",
)


def _paper_test_settings_snapshot(settings: dict) -> dict:
    return {key: settings.get(key) for key in _PAPER_TEST_SETTING_KEYS}


def _apply_paper_test_settings(enabled: bool, *, high_activity: bool = False) -> dict:
    settings = core._load_settings_payload()
    existing_backup = kv_get(_PAPER_SERVICE_TEST_BACKUP_KEY, {})

    if enabled:
        if not isinstance(existing_backup, dict) or not existing_backup:
            kv_set(_PAPER_SERVICE_TEST_BACKUP_KEY, _paper_test_settings_snapshot(settings))
        settings["throughput_auto_scheduler_control"] = True
        settings["relaxed_trade_filters_enabled"] = True
        settings["strict_regime_gating"] = False
        settings["allow_unknown_regime_strategies"] = True
        settings["scanner_signal_interval_minutes"] = 1
        settings["scanner_execution_interval_minutes"] = 1
        settings["paper_test_mode_enabled"] = True
        settings["paper_test_high_activity_enabled"] = bool(high_activity)
        settings["paper_test_bypass_gates_enabled"] = True
        settings["paper_test_local_execution_only"] = True
    else:
        backup = existing_backup if isinstance(existing_backup, dict) else {}
        if backup:
            for key in _PAPER_TEST_SETTING_KEYS:
                if key in backup:
                    settings[key] = backup.get(key)
        else:
            settings["paper_test_mode_enabled"] = False
            settings["paper_test_high_activity_enabled"] = False
            settings["paper_test_bypass_gates_enabled"] = False
            settings["paper_test_local_execution_only"] = True
        kv_set(_PAPER_SERVICE_TEST_BACKUP_KEY, {})

    settings["updated_at"] = _now()
    core._save_settings_payload(settings)
    try:
        from forven.scheduler import apply_runtime_scheduler_overrides

        apply_runtime_scheduler_overrides()
    except Exception as exc:
        log.warning("Could not apply scheduler cadence while toggling paper test mode: %s", exc)
    return settings


def _update_scanner_test_mode_warning(*, active: bool, expires_at: str | None = None) -> None:
    scanner_state = kv_get("scanner_state", {}) or {}
    if not isinstance(scanner_state, dict):
        scanner_state = {}

    warning = None
    if active:
        warning = "Paper test mode is active"
        if expires_at:
            warning = f"{warning}; expires at {expires_at}"

    scanner_state["paper_test_mode"] = bool(active)
    scanner_state["paper_test_warning"] = warning

    signal_summary = scanner_state.get("signal_summary")
    if isinstance(signal_summary, dict):
        signal_summary["paper_test_mode"] = bool(active)
        signal_summary["paper_test_warning"] = warning

    execution_summary = scanner_state.get("execution_summary")
    if isinstance(execution_summary, dict):
        execution_summary["paper_test_mode"] = bool(active)
        execution_summary["paper_test_warning"] = warning

    kv_set("scanner_state", scanner_state)


def _set_paper_scanner_jobs_enabled(enabled: bool) -> None:
    for job_id in ("forven-scanner-signal", "forven-scanner-hourly"):
        try:
            enable_job(job_id, enabled)
        except Exception as exc:
            log.warning("Could not toggle scanner job %s (enabled=%s): %s", job_id, enabled, exc)


def _run_scanner_once(*, execute_positions: bool) -> tuple[bool, str | None]:
    try:
        from forven.scanner import run_scan

        run_scan(execute_positions=execute_positions)
        return True, None
    except Exception as exc:
        log.warning("Paper service scanner kick-off failed: %s", exc)
        return False, str(exc)


def start_paper_service(high_activity_test: bool = False, run_scan_now: bool = True):
    state = kv_get("paper_service_state", {}) or {}
    already_running = bool(state.get("running"))

    if high_activity_test:
        _apply_paper_test_settings(True, high_activity=True)
        state["high_activity_test"] = True
        started_at = datetime.now(timezone.utc)
        expires_at = started_at + _PAPER_TEST_MODE_TTL
        state["high_activity_test_started_at"] = started_at.isoformat()
        state["high_activity_test_expires_at"] = expires_at.isoformat()
        state["high_activity_test_expired_at"] = None
    else:
        state["high_activity_test"] = bool(state.get("high_activity_test", False))

    _set_paper_scanner_jobs_enabled(True)
    state["running"] = True
    state["updated_at"] = _now()
    kv_set("paper_service_state", state)
    _update_scanner_test_mode_warning(
        active=bool(state.get("high_activity_test")),
        expires_at=str(state.get("high_activity_test_expires_at") or "").strip() or None,
    )

    kicked = False
    kick_error = None
    if run_scan_now and (high_activity_test or not already_running):
        kicked, kick_error = _run_scanner_once(execute_positions=True)

    return {
        "status": "running",
        "running": True,
        "high_activity_test": bool(state.get("high_activity_test", False)),
        "high_activity_test_expires_at": state.get("high_activity_test_expires_at"),
        "scanner_jobs_enabled": True,
        "scan_triggered": kicked,
        "scan_error": kick_error,
    }


def stop_paper_service(disable_test_mode: bool = True):
    state = kv_get("paper_service_state", {}) or {}
    state["running"] = False
    if disable_test_mode:
        _apply_paper_test_settings(False)
        state["high_activity_test"] = False
        state["high_activity_test_started_at"] = None
        state["high_activity_test_expires_at"] = None
        state["high_activity_test_expired_at"] = None
    state["updated_at"] = _now()
    _set_paper_scanner_jobs_enabled(False)
    kv_set("paper_service_state", state)
    _update_scanner_test_mode_warning(active=False)
    return {
        "status": "stopped",
        "running": False,
        "high_activity_test": bool(state.get("high_activity_test", False)),
        "scanner_jobs_enabled": False,
    }


__all__ = [
    "_collect_compat_paper_sessions",
    "_find_compat_paper_session",
    "get_paper_session",
    "get_paper_session_indicators",
    "get_paper_session_markers",
    "get_paper_session_replay_bars",
    "get_paper_session_trades",
    "get_paper_sessions",
    "get_paper_summary",
    "start_paper_service",
    "stop_paper_service",
]
