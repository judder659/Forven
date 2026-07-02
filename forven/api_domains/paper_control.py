"""Manual position controls for paper AND live trading sessions.

A "session" is a read-only compat view synthesized from the strategies + trades
tables (see ``api_domains/paper.py``). This module adds the *write* side the UI
needs to actually control a position: close / partial-close / open / adjust-SL /
adjust-TP / flip / pause.

Each control DISPATCHES on whether the position is paper or live:

* **Paper** (``execution_type`` in {paper, paper_challenger, simulation}, no
  exchange order id): local DB writes, filled against the cached daemon mid. No
  exchange interaction.
* **Live** (deployed/graduated strategies, exchange-backed trades): places REAL
  reduce-only / market orders on Hyperliquid via the same primitives the scanner
  uses (``close_position`` / ``market_order`` / ``place_protective_stop`` /
  ``place_take_profit``), then persists the result and frees/registers the risk slot.

Safety rules (operator decisions):
* Closing / reducing / flipping is NEVER gated. Opening a NEW live position
  respects the risk gates (``can_open`` → kill-switch / daily-loss / margin); a
  red gate refuses the open with a clear error.
* Manual live SL/TP are RESTING reduce-only orders on the exchange (true
  protection), tracked by order id in ``signal_data``.
* WS-light: paper paths do one DB txn + a cached mid (no candle loads). Live
  paths are explicit operator actions (rare), so a single exchange round-trip is
  acceptable.
* Clean provenance: every manual write stamps ``signal_data["source"]="manual"``
  and a close reason free of the synthetic tokens
  (reconcile/stale/sweep/unspecified/force) the rollup flags as fabricated:
  ``manual_close`` / ``manual_partial_close`` / ``manual_flip_close``.
"""

import json
import logging

from fastapi import HTTPException

from forven.api_domains import paper as paper_domain
from forven.api_domains import trading as trading_domain
from forven.db import get_db, kv_get, next_container_id
from forven.exchange import books as books_mod
from forven.exchange import risk as risk_mod
from forven.sim.clock import get_now
from forven.trade_state import (
    _coerce_optional_float,
    _normalize_trade_direction,
    close_trade_record,
    is_local_only_paper_trade,
    mark_trade_pending_close_reconcile,
    parse_trade_signal_data,
)

log = logging.getLogger("forven.api")

_INITIAL_PAPER_CAPITAL = 10_000.0
_PAPER_EXECUTION_TYPES = {"paper", "paper_challenger", "simulation"}


def _iso_now() -> str:
    return get_now().isoformat()


# --------------------------------------------------------------------------- #
# Resolution helpers
# --------------------------------------------------------------------------- #
def _session_is_deployed(session: dict) -> bool:
    return str(session.get("compat_kind") or "").strip().lower() == "deployed"


def _resolve_session(session_id: str) -> dict:
    """Resolve a compat session (paper or deployed/live), or raise 404."""
    return paper_domain.get_paper_session(session_id)


def _trade_is_live(trade: dict) -> bool:
    """A live (exchange-backed) trade: not a local-only paper row."""
    exec_type = str(trade.get("execution_type") or "").strip().lower()
    if exec_type in _PAPER_EXECUTION_TYPES:
        return False
    # A non-paper execution_type, or a paper row that carries an exchange order id,
    # is reconcilable against the exchange -> treat as live.
    return not is_local_only_paper_trade(trade)


def _session_is_live(session: dict) -> bool:
    return _session_is_deployed(session)


def _session_open_position(session: dict) -> dict | None:
    position = session.get("position")
    if isinstance(position, dict) and position:
        return position
    positions = session.get("positions")
    if isinstance(positions, list) and positions and isinstance(positions[0], dict):
        return positions[0]
    return None


def _load_open_trade_row(trade_id: str) -> dict | None:
    normalized = str(trade_id or "").strip()
    if not normalized:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE id = ? AND status = 'OPEN'", (normalized,)
        ).fetchone()
    return dict(row) if row else None


def _resolve_open_trade(session_id: str) -> tuple[dict, dict]:
    """Return ``(session, open_trade_row)`` or raise 400 if the session is flat."""
    session = _resolve_session(session_id)
    position = _session_open_position(session)
    trade_id = str((position or {}).get("id") or "").strip()
    trade = _load_open_trade_row(trade_id) if trade_id else None
    if trade is None:
        raise HTTPException(status_code=400, detail="No open position for this session.")
    return session, trade


def _paper_mid(session: dict, trade: dict | None = None) -> float:
    """Light current-price lookup: cached daemon mids only (no candle fetch)."""
    daemon_state = kv_get("daemon_state", {}) or {}
    raw_prices = daemon_state.get("last_prices", {})
    price_map = raw_prices if isinstance(raw_prices, dict) else {}
    mid = paper_domain._resolve_session_current_price(price_map, session.get("symbol"))
    if mid is None or mid <= 0:
        mid = _coerce_optional_float(session.get("current_price"))
    if (mid is None or mid <= 0) and trade is not None:
        mid = (
            _coerce_optional_float(trade.get("fill_entry_price"))
            or _coerce_optional_float(trade.get("entry_price"))
            or _coerce_optional_float(trade.get("signal_entry_price"))
        )
    if mid is None or mid <= 0:
        raise HTTPException(
            status_code=503, detail="No current price available for this symbol."
        )
    return float(mid)


def _fresh_manual_mark(session: dict, trade: dict | None = None) -> float:
    """The price a MANUAL fill uses — a FRESH direct read at click time, so a hand open/close
    lands where the operator sees the price.

    NOT the cached daemon mid (_paper_mid): its updated_at is the daemon's PUBLISH time, blind to
    a stale VALUE (see paper-backstamp-vs-live-fillnow), so when price is moving a manual entry
    landed off the candle it opened on (below the low). One direct venue read per click is fine
    for a user action (unlike the hot close/refresh paths, which stay on the cached mid). Falls
    back to the cached mid when the venue read is unavailable — logged, since a silent fallback
    looks identical to a fresh fill from the caller's side."""
    symbol = str(session.get("symbol") or "").strip().upper()
    asset = (trading_domain._normalize_asset_key(symbol) or symbol.split("/", 1)[0]).strip().upper()
    fallback_reason = "no asset resolved from symbol"
    if asset:
        fallback_reason = None
        try:
            from forven.market_data import resolve_market_data_source

            if resolve_market_data_source() == "binance":
                from forven.market_data import fetch_binance_prices

                prices = fetch_binance_prices([asset])
            else:
                from forven.circuit_breaker import hl_price_breaker
                from forven.exchange.hyperliquid import get_all_mids

                # get_all_mids() itself silently serves the SAME cached daemon mid
                # _paper_mid reads whenever the breaker is open — without raising. Catch
                # that case explicitly here, else a degraded exchange would be reported
                # as a "fresh" fill instead of a fallback.
                if not hl_price_breaker.can_execute():
                    raise RuntimeError("hl_price_breaker is open — venue read unavailable")
                prices = get_all_mids()
            p = _coerce_optional_float((prices or {}).get(asset))
            if p and p > 0:
                return float(p)
            fallback_reason = f"{asset} missing from venue price read"
        except Exception as exc:  # noqa: BLE001 - any venue-read failure falls back to the cached mid
            fallback_reason = f"venue read failed: {exc}"
    log.warning(
        "manual fill: fresh venue price unavailable for %s (%s) — falling back to cached mid",
        asset or symbol, fallback_reason,
    )
    return _paper_mid(session, trade)


def _refresh(session_id: str) -> dict:
    """Return the refreshed compat session so the client updates in one round-trip."""
    return paper_domain.get_paper_session(session_id)


def _update_open_trade_signal_data(
    trade_id: str, updates: dict, removals: tuple[str, ...] = ()
) -> dict:
    with get_db() as conn:
        row = conn.execute(
            "SELECT signal_data FROM trades WHERE id = ? AND status = 'OPEN'",
            (trade_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Position is no longer open.")
        signal_data = parse_trade_signal_data(row["signal_data"])
        signal_data.update(updates)
        for key in removals:
            signal_data.pop(key, None)
        conn.execute(
            "UPDATE trades SET signal_data = ? WHERE id = ?",
            (json.dumps(signal_data), trade_id),
        )
    return signal_data


def _safe_release(trade_id: str) -> None:
    try:
        risk_mod.release(trade_id)
    except Exception:  # noqa: BLE001 - releasing a risk slot is best-effort
        log.warning("release() failed for %s after manual action", trade_id, exc_info=True)


def _live_testnet() -> bool:
    return trading_domain._resolve_exchange_testnet()


def _live_vault_for_trade(trade: dict) -> str | None:
    """Sub-account address an existing live trade routes to (None = master wallet).

    Resolves from the trade's stored direction book via the canonical scanner
    helper with ``strict=True`` so a routed close/adjust on a sub-account position
    fails CLOSED rather than silently downgrading to the master wallet (a
    reduce-only no-op that would strand the real position). Books-disabled trades
    carry book='main'/None and resolve to None (master) — legacy behavior.
    """
    from forven.scanner import _resolve_trade_vault_address

    try:
        return _resolve_trade_vault_address(str(trade.get("id") or ""), strict=True)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"Could not resolve the sub-account for this position: {exc}",
        ) from exc


def _validate_protective_level(kind: str, price: float, mid: float, direction: str) -> None:
    """Reject a stop/target on the wrong side of the market (would fire instantly)."""
    is_long = direction != "short"
    if kind == "stop_loss":
        wrong = price >= mid if is_long else price <= mid
    else:  # take_profit
        wrong = price <= mid if is_long else price >= mid
    if wrong:
        side = "below" if (kind == "stop_loss") == is_long else "above"
        raise HTTPException(
            status_code=400,
            detail=f"{kind.replace('_', ' ')} must be {side} the current price ({mid}).",
        )


# --------------------------------------------------------------------------- #
# Close
# --------------------------------------------------------------------------- #
def _manual_paper_close_pnl_override(trade: dict, exit_price: float) -> dict | None:
    """Net PnL for a manual close of a KERNEL-managed paper trade, using the kernel's
    own cost convention ((price_return*sign*lev - round_trip_drag) * size_fraction).

    Manual closes are operator actions and stay EXCLUDED from the promotion gate (no
    pnl_is_equity_fraction flag, by design — see policy._PARITY_PNL_FILTER), but their
    pnl_usd feeds the paper sandbox equity that sizes every subsequent trade — booking
    them cost-free (the old behaviour) silently inflated the book. Returns None for
    non-kernel rows (close_trade_record's default computation stands)."""
    sd = parse_trade_signal_data(trade.get("signal_data"))
    size_frac = _coerce_optional_float(sd.get("kernel_size_fraction"))
    if not size_frac:
        return None
    entry = _coerce_optional_float(trade.get("fill_entry_price")) or _coerce_optional_float(trade.get("entry_price"))
    if not entry or not exit_price or exit_price <= 0:
        return None
    from forven.db import kv_get

    lev = _coerce_optional_float(trade.get("leverage")) or 1.0
    sign = -1.0 if _normalize_trade_direction(trade.get("direction")) == "short" else 1.0
    settings = kv_get("forven:settings", {}) or {}
    try:
        fee_bps = max(float(settings.get("backtest_fee_bps", 4.5) or 4.5), 0.0)
        slip_bps = max(float(settings.get("backtest_slippage_bps", 2.0) or 2.0), 0.0)
    except (TypeError, ValueError):
        fee_bps, slip_bps = 4.5, 2.0
    drag = 2.0 * (fee_bps + slip_bps) / 10000.0 * max(lev, 0.0)
    pnl_eq = (((float(exit_price) - entry) / entry) * sign * lev - drag) * float(size_frac)
    equity_at_entry = _coerce_optional_float(sd.get("kernel_equity_at_entry")) or 10000.0
    return {
        "net_pnl_pct": round(pnl_eq, 8),
        "pnl_usd": round(equity_at_entry * pnl_eq, 4),
    }


def close_paper_position(session_id: str, reason: str | None = None) -> dict:
    """Close the session's open position (paper: at mid; live: reduce-only market)."""
    session, trade = _resolve_open_trade(session_id)
    note = (str(reason).strip() or None) if reason else None
    if _trade_is_live(trade):
        _live_close_trade(trade, close_reason="manual_close", note=note)
    else:
        mid = _fresh_manual_mark(session, trade)
        closed = close_trade_record(
            str(trade["id"]),
            signal_exit_price=mid,
            exit_price=mid,
            close_reason="manual_close",
            close_price_source="manual_market",
            closed_at=_iso_now(),
            extra_signal_data={
                "source": "manual",
                "manually_closed_at": _iso_now(),
                "manual_close_note": note,
            },
            pnl_override=_manual_paper_close_pnl_override(trade, mid),
        )
        if not closed or not closed.get("updated"):
            raise HTTPException(status_code=502, detail="Failed to close position.")
        _safe_release(str(trade["id"]))
    return _refresh(session_id)


def _live_close_trade(trade: dict, *, close_reason: str, note: str | None = None) -> None:
    """Close a live position with a reduce-only market order, then persist + release."""
    asset = str(trade.get("asset") or "").strip().upper()
    direction = _normalize_trade_direction(trade.get("direction"))
    size = abs(_coerce_optional_float(trade.get("size")) or 0.0)
    if not asset or size <= 0:
        raise HTTPException(status_code=400, detail="Trade is missing asset/size.")
    close_side = "sell" if direction == "long" else "buy"
    testnet = _live_testnet()
    vault = _live_vault_for_trade(trade)

    from forven.exchange.hyperliquid import close_position

    try:
        result = close_position(asset, size, close_side, testnet=testnet, vault_address=vault)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Exchange close failed: {exc}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=502, detail=str(result["error"]))

    order_id = result.get("order_id") or result.get("oid")
    fill = _coerce_optional_float(result.get("exit_price")) or _coerce_optional_float(result.get("fill_price"))
    extra = {
        "source": "manual",
        "manually_closed_at": _iso_now(),
        "manual_close_note": note,
        "exit_exchange_order_id": str(order_id) if order_id is not None else None,
    }
    if fill is not None:
        closed = close_trade_record(
            str(trade["id"]),
            signal_exit_price=fill,
            exit_price=fill,
            close_reason=close_reason,
            close_price_source="manual_live_close",
            closed_at=_iso_now(),
            extra_signal_data=extra,
        )
        if not closed or not closed.get("updated"):
            raise HTTPException(status_code=502, detail="Exchange closed but failed to persist close.")
        _safe_release(str(trade["id"]))
        return

    # No immediate fill — record the requested close and let the reconciler finalize
    # once the exchange confirms flat (mirrors force_close_trade's deferred path).
    pending_price = _coerce_optional_float(result.get("close_price")) or _coerce_optional_float(result.get("mid"))
    pending = mark_trade_pending_close_reconcile(
        str(trade["id"]),
        signal_exit_price=pending_price,
        close_reason=close_reason,
        close_price_source="manual_live_close_requested",
        requested_at=_iso_now(),
        extra_signal_data=extra,
    )
    if not pending or not pending.get("updated"):
        raise HTTPException(status_code=502, detail="Failed to mark live close pending reconciliation.")


# --------------------------------------------------------------------------- #
# Partial close
# --------------------------------------------------------------------------- #
def _resolve_close_qty(qty, pct, size: float) -> float:
    parsed_qty = _coerce_optional_float(qty)
    parsed_pct = _coerce_optional_float(pct)
    if parsed_qty is not None and parsed_qty > 0:
        return min(parsed_qty, size)
    if parsed_pct is not None and parsed_pct > 0:
        return min(size * (parsed_pct / 100.0), size)
    raise HTTPException(status_code=400, detail="Provide qty>0 or pct in (0,100].")


def partial_close_paper_position(session_id: str, qty=None, pct=None) -> dict:
    """Close part of the open position; the residual stays OPEN and strategy-managed."""
    session, trade = _resolve_open_trade(session_id)
    size = abs(_coerce_optional_float(trade.get("size")) or 0.0)
    if size <= 0:
        raise HTTPException(status_code=400, detail="Position has no size to close.")
    # A kernel-managed position is modeled by the execution kernel at its FULL original
    # size; the reconciler has no knowledge of a manual partial, so it would later
    # re-close the whole parent — double-counting the units booked here. Refuse unless
    # the operator has paused (detached) management first.
    _sd = parse_trade_signal_data(trade.get("signal_data"))
    if bool(_sd.get("kernel_managed")) and not bool(_sd.get("manual_pause")):
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot partial-close a strategy-managed position: the execution kernel "
                "would re-close the full original size and double-count. Pause management "
                "for this strategy first, then partial-close."
            ),
        )
    close_qty = _resolve_close_qty(qty, pct, size)
    if close_qty >= size:
        return close_paper_position(session_id, reason="manual_partial_close (full)")

    if _trade_is_live(trade):
        fill = _live_reduce(trade, close_qty)
    else:
        fill = _fresh_manual_mark(session, trade)

    entry = (
        _coerce_optional_float(trade.get("fill_entry_price"))
        or _coerce_optional_float(trade.get("entry_price"))
        or _coerce_optional_float(trade.get("signal_entry_price"))
        or fill
    )
    direction = _normalize_trade_direction(trade.get("direction"))
    leverage = _coerce_optional_float(trade.get("leverage")) or 1.0
    signed = 1.0 if direction == "long" else -1.0
    pnl_usd = (fill - entry) * close_qty * signed
    pnl_pct = ((fill - entry) / entry) * signed * leverage if entry > 0 else 0.0

    parent_id = str(trade["id"])
    closed_at = _iso_now()
    child_signal_data = {
        "source": "manual",
        "close_reason": "manual_partial_close",
        "close_price_source": "manual_live_close" if _trade_is_live(trade) else "manual_market",
        "close_incomplete": False,
        "partial_of": parent_id,
        "manually_closed_at": closed_at,
    }

    with get_db() as conn:
        child_id = next_container_id(conn, "E")
        conn.execute(
            """INSERT INTO trades
            (id, strategy, strategy_id, asset, symbol, direction, entry_price,
             signal_entry_price, exit_price, signal_exit_price, size, risk_pct, leverage,
             pnl, pnl_pct, pnl_usd, status, execution_type, source, signal_data,
             opened_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CLOSED', ?, 'manual', ?, ?, ?)""",
            (
                child_id,
                trade.get("strategy"),
                trade.get("strategy_id"),
                trade.get("asset"),
                trade.get("symbol"),
                direction,
                entry,
                entry,
                fill,
                fill,
                round(close_qty, 8),
                trade.get("risk_pct"),
                leverage,
                round(pnl_usd, 4),
                round(pnl_pct, 6),
                round(pnl_usd, 4),
                str(trade.get("execution_type") or "paper"),
                json.dumps(child_signal_data),
                trade.get("opened_at"),
                closed_at,
            ),
        )

        # Shrink the parent and append an audit entry. The parent's `source` is left
        # untouched: a partial close does not hand the residual to the operator (use
        # Pause for that) — the strategy keeps managing what's left. A live position's
        # resting reduce-only stop is sized at-or-above the residual, so it still
        # protects the smaller position (reduce-only can't exceed it).
        parent_sd = parse_trade_signal_data(trade.get("signal_data"))
        audit = parent_sd.get("partial_closes")
        audit = list(audit) if isinstance(audit, list) else []
        audit.append(
            {
                "child_id": child_id,
                "qty": round(close_qty, 8),
                "exit_price": fill,
                "pnl_usd": round(pnl_usd, 4),
                "at": closed_at,
            }
        )
        parent_sd["partial_closes"] = audit
        conn.execute(
            "UPDATE trades SET size = ?, signal_data = ? WHERE id = ?",
            (round(size - close_qty, 8), json.dumps(parent_sd), parent_id),
        )

    return _refresh(session_id)


def _live_reduce(trade: dict, close_qty: float) -> float:
    """Reduce a live position by ``close_qty`` with a reduce-only market order; return fill."""
    asset = str(trade.get("asset") or "").strip().upper()
    direction = _normalize_trade_direction(trade.get("direction"))
    close_side = "sell" if direction == "long" else "buy"
    testnet = _live_testnet()
    vault = _live_vault_for_trade(trade)
    from forven.exchange.hyperliquid import close_position

    try:
        result = close_position(asset, close_qty, close_side, testnet=testnet, vault_address=vault)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Exchange partial close failed: {exc}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=502, detail=str(result["error"]))
    fill = _coerce_optional_float(result.get("exit_price")) or _coerce_optional_float(result.get("fill_price"))
    if fill is None or fill <= 0:
        raise HTTPException(status_code=502, detail="Partial close did not return a fill price; try again.")
    return float(fill)


# --------------------------------------------------------------------------- #
# Open
# --------------------------------------------------------------------------- #
def open_manual_position(
    session_id: str,
    direction: str,
    size=None,
    risk_pct=None,
    leverage: float = 1.0,
    stop_loss_price=None,
    take_profit_price=None,
) -> dict:
    """Open a brand-new position by hand (paper: local; live: real market order)."""
    session = _resolve_session(session_id)
    if _session_open_position(session) is not None:
        raise HTTPException(status_code=409, detail="A position is already open for this session.")

    norm_dir = _normalize_trade_direction(direction)
    strategy_id = str(session.get("strategy_id") or "").strip()
    if not strategy_id:
        raise HTTPException(status_code=400, detail="Session has no strategy id.")
    symbol = str(session.get("symbol") or "").strip().upper()
    asset = trading_domain._normalize_asset_key(symbol) or symbol.split("/", 1)[0]

    mid = _fresh_manual_mark(session)
    lev = _coerce_optional_float(leverage) or 1.0
    if lev <= 0:
        lev = 1.0
    sl = _coerce_optional_float(stop_loss_price)
    tp = _coerce_optional_float(take_profit_price)
    if sl is not None and sl > 0:
        _validate_protective_level("stop_loss", sl, mid, norm_dir)
    if tp is not None and tp > 0:
        _validate_protective_level("take_profit", tp, mid, norm_dir)

    if _session_is_live(session):
        _live_open(
            session_id, strategy_id, asset, norm_dir,
            size=size, risk_pct=risk_pct, leverage=lev,
            stop_loss_price=sl, take_profit_price=tp,
        )
        return _refresh(session_id)

    # ── Paper open ──
    resolved_size = _coerce_optional_float(size)
    resolved_risk_pct = _coerce_optional_float(risk_pct)
    sizing_meta = None
    if resolved_size is None or resolved_size <= 0:
        if resolved_risk_pct is None or resolved_risk_pct <= 0:
            raise HTTPException(status_code=400, detail="Provide size>0 or risk_pct in (0,100].")
        # Size via the SHARED sizing mirror (forven.strategies.sizing) — the same
        # fraction math the kernel/auto path uses — so a manual open is consistent
        # with how the engine would size it: risk risk_pct of equity over the stop.
        from forven.strategies import sizing as _sizing

        equity = _coerce_optional_float(session.get("capital")) or _INITIAL_PAPER_CAPITAL
        risk_frac = resolved_risk_pct / 100.0
        stop_dist_pct = (abs(mid - sl) / mid) if (sl and sl > 0 and mid) else None
        ec = _sizing.default_controls(risk_frac)
        size_fraction = _sizing.size_fraction(ec, stop_dist_pct, leverage=lev, initial_capital=equity)
        resolved_size = round(_sizing.position_units(equity=equity, size_fraction=size_fraction, leverage=lev, entry_price=mid), 6)
        sizing_meta = {
            "method": "fraction_mirror", "size_fraction": round(float(size_fraction), 8),
            "units": resolved_size, "portfolio_equity": round(float(equity), 4),
            "leverage": lev, "stop_distance_pct": (round(float(stop_dist_pct), 8) if stop_dist_pct else None),
            "risk_pct": resolved_risk_pct, "mirror_sized": True,
        }
    if resolved_size is None or resolved_size <= 0:
        raise HTTPException(status_code=400, detail="Computed position size is zero.")

    signal_data: dict = {"source": "manual", "opened_manually_at": _iso_now()}
    if sl is not None and sl > 0:
        signal_data["stop_loss_price"] = float(sl)
        signal_data["stop_loss_source"] = "manual"
    if tp is not None and tp > 0:
        signal_data["take_profit_price"] = float(tp)
        signal_data["take_profit_source"] = "manual"
    if sizing_meta:
        signal_data["manual_sizing"] = sizing_meta

    trade_id = _open_trade_db_safe(
        strategy_id=strategy_id, asset=asset, direction=norm_dir, entry=mid,
        size=float(resolved_size), risk_pct=float((resolved_risk_pct or 0.0) / 100.0),
        leverage=lev, signal_data=signal_data, execution_type="paper",
    )
    log.info("Manual paper open %s %s %s size=%s @ %s", trade_id, norm_dir, asset, resolved_size, mid)
    return _refresh(session_id)


def _live_open(
    session_id, strategy_id, asset, direction, *, size, risk_pct, leverage,
    stop_loss_price, take_profit_price,
) -> None:
    """Open a real Hyperliquid position (gated), persist it, and register the slot."""
    risk_fraction = None
    parsed_risk = _coerce_optional_float(risk_pct)
    if parsed_risk is not None and parsed_risk > 0:
        risk_fraction = parsed_risk / 100.0

    # Route the new position to its direction book (Approach C sub-account). With
    # books disabled this is ("main", None) -> master wallet (legacy). In long-only
    # mode a short open is skipped with an operator-facing reason.
    book, skip_reason = books_mod.resolve_open_book(direction)
    if skip_reason:
        raise HTTPException(status_code=409, detail=skip_reason)
    vault = books_mod.book_address(book)

    # Gate: opening a NEW live position respects the risk gates (kill-switch / daily
    # loss / margin), scoped to the routed book. Closing/reducing is never gated.
    allowed, allocated_risk, reason = risk_mod.can_open(
        asset, direction, strategy_id, risk_fraction, execution_type="live", book=book
    )
    if not allowed:
        raise HTTPException(status_code=409, detail=f"Blocked by risk gate: {reason}")

    # RISK-3: a real live position MUST carry a protective stop. The automated
    # engine refuses a stopless open (_execute_direct raises "refusing to open
    # without a protective stop"); a hand-opened naked live position is an
    # unbounded-loss hole, so refuse it here too instead of placing a bare
    # market_order with stop_loss_price=None. (Checked after the risk gate so the
    # kill-switch / long-only / halt blocks still take precedence.)
    _parsed_stop = _coerce_optional_float(stop_loss_price)
    if _parsed_stop is None or _parsed_stop <= 0:
        raise HTTPException(status_code=400, detail="A live position requires a protective stop_loss_price.")

    testnet = _live_testnet()
    from forven.exchange.hyperliquid import get_account_value, market_order

    resolved_size = _coerce_optional_float(size)
    if resolved_size is None or resolved_size <= 0:
        if risk_fraction is None:
            raise HTTPException(status_code=400, detail="Provide size>0 or risk_pct in (0,100].")
        try:
            equity = float(get_account_value(testnet=testnet) or 0.0)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Could not read live equity for sizing: {exc}") from exc
        if equity <= 0:
            raise HTTPException(status_code=502, detail="Live account equity is zero; cannot size by risk %.")
        mid = _fresh_manual_mark(_resolve_session(session_id))
        resolved_size, _ = risk_mod.calculate_position_size(
            asset=asset, direction=direction, entry_price=mid,
            stop_loss_price=stop_loss_price, account_equity=equity,
            risk_pct=allocated_risk or risk_fraction, leverage=leverage,
        )
    if resolved_size is None or resolved_size <= 0:
        raise HTTPException(status_code=400, detail="Computed position size is zero.")

    # H8 (RISK-3): re-assert the trading halt at EXECUTION time. can_open checked it
    # above, but the kill-switch / daily-loss halt may have fired in the window
    # since — the automated _execute_direct path does the same re-assert.
    _halt_ok, _halt_reason = risk_mod.is_trading_allowed()
    if not _halt_ok:
        raise HTTPException(status_code=409, detail=f"Trading halted — {_halt_reason}")

    side = "buy" if direction == "long" else "sell"
    try:
        result = market_order(
            asset, side, float(resolved_size),
            stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
            testnet=testnet, vault_address=vault,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Exchange open failed: {exc}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=502, detail=str(result["error"]))

    fill = _coerce_optional_float(result.get("entry_price")) or _fresh_manual_mark(_resolve_session(session_id))
    filled_size = _coerce_optional_float(result.get("filled_size")) or float(resolved_size)
    entry_oid = result.get("entry_order_id") or result.get("order_id")
    stop_oid = result.get("stop_order_id")
    tp_oid = result.get("take_profit_order_id")

    signal_data: dict = {
        "source": "manual",
        "opened_manually_at": _iso_now(),
        "entry_exchange_order_id": str(entry_oid) if entry_oid is not None else None,
        "exchange_order_id": str(entry_oid) if entry_oid is not None else None,
    }
    if stop_loss_price:
        signal_data["stop_loss_price"] = float(stop_loss_price)
        signal_data["stop_loss_source"] = "manual"
        signal_data["exchange_stop_requested"] = True
        if stop_oid is not None:
            signal_data["exchange_stop_order_id"] = str(stop_oid)
    if take_profit_price:
        signal_data["take_profit_price"] = float(take_profit_price)
        signal_data["take_profit_source"] = "manual"
        if tp_oid is not None:
            signal_data["exchange_take_profit_order_id"] = str(tp_oid)

    trade_id = _open_trade_db_safe(
        strategy_id=strategy_id, asset=asset, direction=direction, entry=fill,
        size=float(filled_size), risk_pct=float(risk_fraction or 0.0),
        leverage=leverage, signal_data=signal_data, execution_type="live", book=book,
    )
    try:
        from forven.scanner import _update_trade_fill

        _update_trade_fill(
            trade_id=trade_id, fill_price=fill, fill_kind="entry",
            signal_price=fill,
            exchange_order_id=str(entry_oid) if entry_oid is not None else None,
            filled_size=filled_size,
        )
    except Exception:  # noqa: BLE001 - fill bookkeeping is best-effort; entry_price is already set
        log.warning("manual live open %s: fill bookkeeping failed", trade_id, exc_info=True)
    try:
        risk_mod.register(
            trade_id, asset, direction, strategy_id, float(risk_fraction or 0.0),
            float(fill), execution_type="live", book=book,
        )
    except Exception:  # noqa: BLE001
        log.warning("manual live open %s: risk register failed", trade_id, exc_info=True)
    # #4: a protective leg the exchange REJECTED on the bracket entry (entry filled, stop
    # bounced) must not be recorded as a normally-protected position. Re-arm it immediately
    # (and alert if the stop still can't be armed) — mirroring the scanner's open path.
    from forven.sim.clock import is_sim_active

    _failed_legs = result.get("protective_leg_failed") or []
    if _failed_legs and not is_sim_active():
        _arm_failed_protective_legs(
            trade_id, asset, direction, float(filled_size), _failed_legs,
            stop_loss_price=stop_loss_price, take_profit_price=take_profit_price,
            testnet=testnet, vault=vault,
        )
    log.info("Manual LIVE open %s %s %s size=%s @ %s", trade_id, direction, asset, filled_size, fill)


def _open_trade_db_safe(
    *, strategy_id, asset, direction, entry, size, risk_pct, leverage, signal_data,
    execution_type, book=None,
) -> str:
    """Open a trade via the canonical path (unique-open index = one-per-asset)."""
    from forven.scanner import _open_trade_db

    try:
        return _open_trade_db(
            strat_id=strategy_id, asset=asset, direction=direction, entry=entry,
            size=size, risk_pct=risk_pct, leverage=leverage,
            signal_data=signal_data, execution_type=execution_type, book=book,
        )
    except Exception as exc:  # noqa: BLE001
        if "idx_trades_unique_open" in str(exc):
            raise HTTPException(
                status_code=409, detail="A position is already open for this strategy/asset."
            ) from exc
        raise HTTPException(status_code=502, detail=f"Failed to open position: {exc}") from exc


# --------------------------------------------------------------------------- #
# Adjust stop-loss / take-profit
# --------------------------------------------------------------------------- #
def adjust_stop_loss(session_id: str, price) -> dict:
    """Set or clear (price=None) the stop-loss. Live: resting reduce-only stop order."""
    session, trade = _resolve_open_trade(session_id)
    return _adjust_protective(session_id, session, trade, "stop_loss", price)


def adjust_take_profit(session_id: str, price) -> dict:
    """Set or clear (price=None) the take-profit. Live: resting reduce-only TP order."""
    session, trade = _resolve_open_trade(session_id)
    return _adjust_protective(session_id, session, trade, "take_profit", price)


_PROTECTIVE_FIELDS = {
    "stop_loss": ("stop_loss_price", "stop_loss_source", "sl_adjusted_at", "exchange_stop_order_id"),
    "take_profit": ("take_profit_price", "take_profit_source", "tp_adjusted_at", "exchange_take_profit_order_id"),
}


def _adjust_protective(session_id: str, session: dict, trade: dict, kind: str, price) -> dict:
    price_key, source_key, ts_key, oid_key = _PROTECTIVE_FIELDS[kind]
    parsed = _coerce_optional_float(price)
    trade_id = str(trade["id"])
    direction = _normalize_trade_direction(trade.get("direction"))
    live = _trade_is_live(trade)
    sd = parse_trade_signal_data(trade.get("signal_data"))
    existing_oid = sd.get(oid_key)
    vault = _live_vault_for_trade(trade) if live else None

    if parsed is None:
        # Clear the level (and cancel the resting exchange order, if live).
        if live and existing_oid:
            _cancel_live_order(trade.get("asset"), existing_oid, vault)
        _update_open_trade_signal_data(
            trade_id, {source_key: "manual", ts_key: _iso_now()}, removals=(price_key, oid_key)
        )
        return _refresh(session_id)

    if parsed <= 0:
        raise HTTPException(status_code=400, detail=f"{kind.replace('_', ' ')} must be > 0 (or null to clear).")
    mid = _paper_mid(session, trade)
    _validate_protective_level(kind, parsed, mid, direction)

    updates = {price_key: float(parsed), source_key: "manual", ts_key: _iso_now()}
    if live:
        # Replace the resting exchange order on the position's sub-account: cancel
        # the old one, place the new.
        if existing_oid:
            _cancel_live_order(trade.get("asset"), existing_oid, vault)
        new_oid = _place_live_protective(kind, trade, float(parsed), vault)
        if new_oid is not None:
            updates[oid_key] = str(new_oid)
        else:
            updates.pop(oid_key, None)
        if kind == "stop_loss":
            updates["exchange_stop_requested"] = True
    _update_open_trade_signal_data(trade_id, updates)
    return _refresh(session_id)


def _cancel_live_order(asset, oid, vault_address: str | None = None) -> None:
    try:
        from forven.exchange.hyperliquid import cancel_order

        cancel_order(str(asset).upper(), int(oid), testnet=_live_testnet(), vault_address=vault_address)
    except Exception:  # noqa: BLE001 - a stale/already-filled order is fine to ignore
        log.warning("cancel_order(%s, %s) failed during manual adjust", asset, oid, exc_info=True)


def _place_live_protective(kind: str, trade: dict, price: float, vault_address: str | None = None):
    asset = str(trade.get("asset") or "").strip().upper()
    direction = _normalize_trade_direction(trade.get("direction"))
    size = abs(_coerce_optional_float(trade.get("size")) or 0.0)
    if size <= 0:
        raise HTTPException(status_code=400, detail="Position has no size to protect.")
    testnet = _live_testnet()
    from forven.exchange.hyperliquid import place_protective_stop, place_take_profit

    placer = place_protective_stop if kind == "stop_loss" else place_take_profit
    try:
        result = placer(asset, direction, size, price, testnet=testnet, vault_address=vault_address)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Exchange {kind} placement failed: {exc}") from exc
    if isinstance(result, dict) and result.get("error"):
        raise HTTPException(status_code=502, detail=str(result["error"]))
    return result.get("stop_order_id") or result.get("take_profit_order_id") or result.get("order_id")


def _arm_failed_protective_legs(
    trade_id: str, asset: str, direction: str, size: float, failed_legs: list,
    *, stop_loss_price: float | None, take_profit_price: float | None,
    testnet: bool, vault: str | None,
) -> None:
    """#4: a bracket entry FILLED but a protective leg was REJECTED
    (result.protective_leg_failed) — the position is open but, for the stop, UNPROTECTED.
    Mirror the scanner's open path: re-arm the failed leg immediately; if the STOP cannot be
    armed, flag it unarmed + queue a reconcile repair and raise a CRITICAL operator alert so a
    hand-opened position is never silently recorded as protected when its stop bounced."""
    from forven.exchange.hyperliquid import place_protective_stop, place_take_profit

    kw: dict = {"testnet": testnet}
    if vault:
        kw["vault_address"] = vault

    if "stop" in failed_legs and stop_loss_price:
        try:
            ps = place_protective_stop(asset, direction, size, float(stop_loss_price), **kw)
        except Exception as exc:  # noqa: BLE001
            ps = {"error": str(exc)}
        if isinstance(ps, dict) and not ps.get("error") and ps.get("stop_order_id"):
            _update_open_trade_signal_data(trade_id, {
                "exchange_stop_order_id": str(ps["stop_order_id"]), "protective_stop_rearmed": True,
            })
        else:
            err = (ps or {}).get("error") if isinstance(ps, dict) else None
            _update_open_trade_signal_data(trade_id, {
                "protective_stop_unarmed": True,
                "pending_open_reconcile": True, "pending_open_reconcile_at": _iso_now(),
            })
            log.error(
                "manual live open %s: entry FILLED but stop leg rejected AND re-arm failed: %s — "
                "reconcile will retry", trade_id, err,
            )
            try:
                from forven.notifications import emit_notification
                emit_notification(
                    "trade_protective_unarmed", severity="critical", source="manual",
                    title=f"Live position temporarily UNPROTECTED ({asset})",
                    summary=f"{asset} {direction} manual entry filled but the protective stop could "
                            "not be armed; reconcile will retry.",
                    body=f"trade={trade_id}: {err}",
                    dedupe_key=f"protective_unarmed:{trade_id}",
                )
            except Exception:
                pass

    if "take_profit" in failed_legs and take_profit_price:
        try:
            tp = place_take_profit(asset, direction, size, float(take_profit_price), **kw)
        except Exception:  # noqa: BLE001
            tp = None
        if isinstance(tp, dict) and not tp.get("error") and tp.get("take_profit_order_id"):
            _update_open_trade_signal_data(trade_id, {
                "exchange_take_profit_order_id": str(tp["take_profit_order_id"]), "protective_tp_rearmed": True,
            })


# --------------------------------------------------------------------------- #
# Flip
# --------------------------------------------------------------------------- #
def flip_position(session_id: str) -> dict:
    """Close the open position and re-open the opposite side at the same size."""
    session, trade = _resolve_open_trade(session_id)
    old_id = str(trade["id"])
    direction = _normalize_trade_direction(trade.get("direction"))
    opposite = "short" if direction == "long" else "long"
    size = abs(_coerce_optional_float(trade.get("size")) or 0.0)
    if size <= 0:
        raise HTTPException(status_code=400, detail="Position has no size to flip.")
    leverage = _coerce_optional_float(trade.get("leverage")) or 1.0
    risk_pct = _coerce_optional_float(trade.get("risk_pct")) or 0.0
    strategy_id = str(trade.get("strategy_id") or trade.get("strategy"))
    asset = str(trade.get("asset"))
    live = _trade_is_live(trade)

    if live:
        # Pre-flight BOTH the routing (long-only short-skip) and the open-side gate
        # BEFORE closing, so neither leaves the position flat (gates/skip block opens,
        # never closes).
        open_book, skip_reason = books_mod.resolve_open_book(opposite)
        if skip_reason:
            raise HTTPException(status_code=409, detail=f"Flip blocked: {skip_reason}")
        allowed, _, reason = risk_mod.can_open(
            asset, opposite, strategy_id, None, execution_type="live", book=open_book
        )
        if not allowed:
            raise HTTPException(status_code=409, detail=f"Flip blocked by risk gate: {reason}")
        # _live_open REQUIRES a protective stop (RISK-3): a bare None stop 400s AFTER the
        # close and strands the account FLAT instead of reversing. Derive a re-anchored
        # stop/target for the REVERSED side from the strategy's execution profile at the
        # current mark, computed BEFORE closing — so a non-derivable stop refuses the flip
        # rather than closing and then failing the re-open.
        rev_mark = _fresh_manual_mark(session, trade)
        rev_levels = _profile_levels_for_trade(
            {"entry_price": rev_mark, "direction": opposite, "asset": asset,
             "entry_time": _iso_now(), "created_at": _iso_now()},
            _strategy_execution_controls(strategy_id), _strategy_timeframe(strategy_id),
        )
        rev_stop = _coerce_optional_float(rev_levels.get("stop_loss"))
        if rev_stop is None or rev_stop <= 0:
            raise HTTPException(
                status_code=409,
                detail="Flip blocked: could not derive a protective stop for the reversed "
                       "position; not closing (a live position must carry a stop).",
            )
        _live_close_trade(trade, close_reason="manual_flip_close")
        _live_open(
            session_id, strategy_id, asset, opposite,
            size=size, risk_pct=None, leverage=leverage,
            stop_loss_price=rev_stop,
            take_profit_price=_coerce_optional_float(rev_levels.get("take_profit")),
        )
        return _refresh(session_id)

    # ── Paper flip ──
    mid = _fresh_manual_mark(session, trade)
    closed = close_trade_record(
        old_id, signal_exit_price=mid, exit_price=mid,
        close_reason="manual_flip_close", close_price_source="manual_market", closed_at=_iso_now(),
        extra_signal_data={"source": "manual", "manually_closed_at": _iso_now(), "manual_flip": True},
    )
    if not closed or not closed.get("updated"):
        raise HTTPException(status_code=502, detail="Failed to close position for flip.")
    _safe_release(old_id)
    new_id = _open_trade_db_safe(
        strategy_id=strategy_id, asset=asset, direction=opposite, entry=mid,
        size=size, risk_pct=float(risk_pct), leverage=leverage,
        signal_data={"source": "manual", "opened_manually_at": _iso_now(), "flipped_from": old_id},
        execution_type="paper",
    )
    log.info("Manual paper flip %s -> %s (%s)", old_id, new_id, opposite)
    return _refresh(session_id)


# --------------------------------------------------------------------------- #
# Pause / resume auto-management (no exchange interaction)
# --------------------------------------------------------------------------- #
def set_manual_pause(session_id: str, paused: bool) -> dict:
    """Pause/resume scanner auto-management for the open position (full detach)."""
    session, trade = _resolve_open_trade(session_id)
    _update_open_trade_signal_data(
        str(trade["id"]),
        {"manual_pause": bool(paused), "manual_pause_set_at": _iso_now()},
    )
    return _refresh(session_id)


# --------------------------------------------------------------------------- #
# Propagate execution-profile edits onto an OPEN position
# When an operator changes a paper/live strategy's execution settings (Save /
# Set-default), the open position's SL/TP/trailing are recomputed from the new
# profile — anchored at the ENTRY price and derived EXACTLY as the kernel places
# them (sizing.entry_stop_dist_pct) — so the immediate apply matches what the next
# scan would refresh to (no fight with auto-management). Paper: signal_data DB
# write. Live: cancel + re-place the resting reduce-only exchange orders.
# --------------------------------------------------------------------------- #
def _strategy_timeframe(strategy_id: str) -> str:
    try:
        with get_db() as conn:
            row = conn.execute("SELECT timeframe FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        tf = str((dict(row).get("timeframe") if row else "") or "").strip().lower()
        return tf or "1h"
    except Exception:
        return "1h"


def _strategy_execution_controls(strategy_id: str) -> dict:
    """The strategy's normalized execution controls from its stored execution_profile,
    falling back to default_controls() — which always carries DEFAULT_STOP_LOSS_PCT_FLOOR,
    so a protective stop is always derivable (used to re-anchor the reversed side on a flip)."""
    from forven.strategies import sizing as _sizing

    params: dict = {}
    try:
        with get_db() as conn:
            row = conn.execute("SELECT params FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        raw = dict(row).get("params") if row is not None else None
        if raw:
            params = json.loads(raw) or {}
    except Exception:
        params = {}
    return _sizing.normalize_execution_controls(_sizing.extract_execution_profile(params)) or _sizing.default_controls()


def _atr_at_entry(asset: str, timeframe: str, entry_time, period: int) -> float | None:
    """Wilder ATR at the position's entry bar — matches how the kernel placed the
    stop. Falls back to the latest ATR if the entry bar is outside the fetch window."""
    import pandas as pd

    from forven.market_data import fetch_market_candles
    from forven.strategies.execution_kernel import _compute_atr_series

    try:
        df = fetch_market_candles(asset, bars=max(int(period) * 6, 300), interval=timeframe)
        if df is None or df.empty:
            return None
        atr = _compute_atr_series(df, int(period))
        if entry_time:
            try:
                ts = pd.Timestamp(str(entry_time))
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                pos = atr.index.get_indexer([ts], method="nearest")[0]
                if pos >= 0:
                    return float(atr.iloc[pos])
            except Exception:
                pass
        return float(atr.iloc[-1])
    except Exception:
        return None


def _profile_levels_for_trade(trade: dict, ec: dict, timeframe: str) -> dict:
    """The SL/TP/trailing the new execution profile implies for this OPEN position,
    anchored at its ENTRY price (exactly as the kernel places them at entry)."""
    from forven.strategies import sizing as _sizing

    entry = _coerce_optional_float(trade.get("entry_price")) or 0.0
    direction = _normalize_trade_direction(trade.get("direction"))
    sign = -1.0 if direction == "short" else 1.0
    asset = str(trade.get("asset") or "").strip().upper()

    atr_value = None
    if ec.get("sizing_mode") == "atr":
        atr_value = _atr_at_entry(asset, timeframe, trade.get("entry_time") or trade.get("created_at"), ec.get("atr_period", 14))

    stop_dist = _sizing.entry_stop_dist_pct(ec, entry_price=entry, atr_value=atr_value) if entry > 0 else None
    new_sl = None
    if stop_dist is not None and (ec.get("stop_loss_pct") is not None or ec.get("sizing_mode") == "atr"):
        new_sl = round(entry * (1.0 - sign * stop_dist), 8)
    new_tp = None
    if ec.get("take_profit_pct") is not None:
        new_tp = round(entry * (1.0 + sign * float(ec["take_profit_pct"]) / 100.0), 8)
    new_trail = float(ec["trailing_stop_pct"]) if ec.get("trailing_stop_pct") is not None else None
    return {"stop_loss": new_sl, "take_profit": new_tp, "trailing_stop_pct": new_trail}


def _apply_levels_to_open_trade(trade: dict, levels: dict) -> dict:
    """Apply recomputed SL/TP/trailing to one OPEN trade (paper DB / live exchange)."""
    trade_id = str(trade.get("id"))
    asset = str(trade.get("asset") or "").strip().upper()
    direction = _normalize_trade_direction(trade.get("direction"))
    live = _trade_is_live(trade)
    sd = parse_trade_signal_data(trade.get("signal_data"))
    vault = _live_vault_for_trade(trade) if live else None

    new_sl = levels.get("stop_loss")
    new_tp = levels.get("take_profit")
    new_trail = levels.get("trailing_stop_pct")
    old_sl = _coerce_optional_float(sd.get("stop_loss_price") if sd.get("stop_loss_price") is not None else sd.get("stop_loss"))
    old_tp = _coerce_optional_float(sd.get("take_profit_price") if sd.get("take_profit_price") is not None else sd.get("take_profit"))

    updates: dict = {}
    if new_sl is not None and new_sl > 0:
        applied = True
        if live:
            old_oid = sd.get("exchange_stop_order_id")
            # PLACE-BEFORE-CANCEL: confirm the NEW resting stop on the exchange BEFORE
            # cancelling the old one. If the replacement is rejected (returns None or raises),
            # KEEP the old stop — never leave the position unprotected on a failed re-place —
            # and surface the failure instead of recording a stop the exchange doesn't have.
            new_oid = None
            try:
                new_oid = _place_live_protective("stop_loss", trade, float(new_sl), vault)
            except HTTPException as exc:
                log.warning("[%s] live stop replace rejected (%s); keeping the existing stop", trade_id, exc.detail)
            if new_oid is None:
                applied = False
                updates["stop_loss_replace_failed"] = True
                updates["stop_loss_replace_failed_at"] = _iso_now()
            else:
                if old_oid:
                    _cancel_live_order(asset, old_oid, vault)
                updates["exchange_stop_order_id"] = str(new_oid)
                updates["exchange_stop_requested"] = True
        if applied:
            updates["stop_loss"] = float(new_sl)
            updates["stop_loss_price"] = float(new_sl)
            updates["stop_loss_source"] = "execution_profile"
            updates["sl_adjusted_at"] = _iso_now()

    if new_tp is not None and new_tp > 0:
        applied_tp = True
        if live:
            old_oid = sd.get("exchange_take_profit_order_id")
            # Same place-before-cancel ordering for the take-profit leg.
            new_oid = None
            try:
                new_oid = _place_live_protective("take_profit", trade, float(new_tp), vault)
            except HTTPException as exc:
                log.warning("[%s] live take-profit replace rejected (%s); keeping the existing TP", trade_id, exc.detail)
            if new_oid is None:
                applied_tp = False
            else:
                if old_oid:
                    _cancel_live_order(asset, old_oid, vault)
                updates["exchange_take_profit_order_id"] = str(new_oid)
        if applied_tp:
            updates["take_profit"] = float(new_tp)
            updates["take_profit_price"] = float(new_tp)
            updates["take_profit_source"] = "execution_profile"
            updates["tp_adjusted_at"] = _iso_now()

    if new_trail is not None and new_trail > 0:
        updates["trailing_stop_pct"] = float(new_trail)

    if updates:
        _update_open_trade_signal_data(trade_id, updates)

    return {
        "trade_id": trade_id, "asset": asset, "direction": direction, "is_live": live,
        "entry_price": _coerce_optional_float(trade.get("entry_price")),
        "stop_loss": {"old": old_sl, "new": new_sl},
        "take_profit": {"old": old_tp, "new": new_tp},
        "trailing_stop_pct": new_trail,
    }


def open_position_summary(strategy_id: str) -> dict:
    """Lightweight 'is this strategy in a trade?' check for the pre-edit warning.
    Returns the open position(s) with current entry/SL/TP so the UI can warn before
    an execution-setting change touches them."""
    from forven.scanner import _get_open_trades

    trades = [t for t in _get_open_trades(strategy_id) if str(t.get("status") or "").upper() == "OPEN"]
    positions = []
    for trade in trades:
        sd = parse_trade_signal_data(trade.get("signal_data"))
        positions.append({
            "trade_id": str(trade.get("id")),
            "asset": str(trade.get("asset") or "").strip().upper(),
            "direction": _normalize_trade_direction(trade.get("direction")),
            "is_live": _trade_is_live(trade),
            "entry_price": _coerce_optional_float(trade.get("entry_price")),
            "stop_loss": _coerce_optional_float(sd.get("stop_loss_price") if sd.get("stop_loss_price") is not None else sd.get("stop_loss")),
            "take_profit": _coerce_optional_float(sd.get("take_profit_price") if sd.get("take_profit_price") is not None else sd.get("take_profit")),
        })
    return {"has_open_position": bool(positions), "count": len(positions), "positions": positions}


def apply_execution_profile_to_open_position(strategy_id: str, params: dict, *, actor: str = "ui") -> dict | None:
    """Push the new execution_profile's SL/TP/trailing onto the strategy's OPEN
    position(s). Returns an impact summary, or None if nothing is open. Best-effort:
    a per-trade failure is recorded, never raised, so the param save never fails on a
    downstream exchange hiccup."""
    from forven.scanner import _get_open_trades
    from forven.strategies import sizing as _sizing

    open_trades = [t for t in _get_open_trades(strategy_id) if str(t.get("status") or "").upper() == "OPEN"]
    if not open_trades:
        return None

    ec = _sizing.normalize_execution_controls(_sizing.extract_execution_profile(params)) or _sizing.default_controls()
    timeframe = _strategy_timeframe(strategy_id)
    positions = []
    for trade in open_trades:
        try:
            levels = _profile_levels_for_trade(trade, ec, timeframe)
            positions.append(_apply_levels_to_open_trade(trade, levels))
        except Exception as exc:  # noqa: BLE001 — never fail the param save on a downstream hiccup
            log.warning("apply execution profile to open trade %s failed: %s", trade.get("id"), exc, exc_info=True)
            positions.append({"trade_id": str(trade.get("id")), "error": str(exc)})
    return {"affected": True, "count": len(positions), "positions": positions}
