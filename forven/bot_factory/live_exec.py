"""Bot Factory LIVE execution — the sanctioned live path, nothing else.

A live-armed bot places REAL Hyperliquid orders through exactly the same gate
stack as the strategy scanner's kernel live path (_kernel_open_live_trade):

    direction-book routing → can_open (kill-switch / daily-halt / margin /
    one-per-asset / cooldown) → check_live_portfolio_budget (SIZE-CAP-1 /
    BOOK-BUDGET-1 / PORT-1 / CORR-1) → check_live_strategy_ceiling (the per-bot
    GO-LIVE ceiling, REQUIRED for bots) → recorded OPEN + risk registration →
    _execute_direct (halt re-assert, leverage, market order with a MANDATORY
    protective stop; LIQ-1 liquidity guard inside market_order).

Everything fails closed: no equity snapshot, no ceiling, no stop, or any gate
refusal blocks the open. Closes are real reduce-only orders finalized like the
kernel live close (fill-based P&L, protection-order retirement, risk-slot
release); the bot's realized-P&L ledger is credited at the close choke-point
in trade_state.close_trade_record.

This module runs INSIDE the bot subprocess. Exchange credentials come from the
encrypted KV settings store (or forwarded env vars for live-armed bots — see
manager._build_isolated_env); scanner/risk state is all DB/KV-backed, so the
gate stack behaves identically here and in the backend process.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)


def pair_to_coin(ticker: str) -> str:
    """Map a bot market-data pair ("BTC/USDT") to the exchange coin ("BTC")."""
    raw = str(ticker or "").strip().upper()
    return raw.split("/", 1)[0] if "/" in raw else raw


def _sl_tp_prices(
    entry: float, direction: str, sl_pct, tp_pct
) -> tuple[float | None, float | None]:
    """Protective stop / take-profit levels from the bot's configured pcts."""
    sgn = -1.0 if direction == "short" else 1.0
    stop = round(entry * (1.0 - sgn * float(sl_pct) / 100.0), 8) if sl_pct else None
    target = round(entry * (1.0 + sgn * float(tp_pct) / 100.0), 8) if tp_pct else None
    return stop, target


def open_live(
    bot_config: dict,
    *,
    ticker: str,
    direction: str,
    qty: float,
    ref_price: float,
    reasoning: str | None = None,
) -> tuple[dict | None, str]:
    """Open a REAL position for a bot through the full live gate stack.

    Returns (position_dict, message); position_dict is None when the open was
    blocked or failed, with the reason in message. The position dict mirrors
    the runner's in-memory shape (trade_id / ticker / direction / qty /
    entry_price / stop_loss_price / take_profit_price).
    """
    from forven.db import execute_bot_trade, get_db
    from forven.exchange import books
    from forven.exchange.risk import (
        can_open,
        check_live_portfolio_budget,
        check_live_strategy_ceiling,
        get_live_notional_ceilings,
        register,
    )
    from forven.scanner import (
        _book_account_equity,
        _execute_direct,
        _get_real_account_equity,
        _opposite_book_would_cross,
        _report_execution_failure,
    )

    bot_id = str(bot_config.get("id") or "")
    sid = f"bot:{bot_id}"
    coin = pair_to_coin(ticker)
    direction = "short" if str(direction or "").strip().lower() == "short" else "long"
    try:
        qty = float(qty)
        ref_price = float(ref_price)
    except (TypeError, ValueError):
        return None, "blocked: unpriceable order (bad qty/price)"
    if not coin or qty <= 0 or ref_price <= 0:
        return None, "blocked: unpriceable order (bad qty/price)"

    # A live order must carry a protective stop — _execute_direct and
    # market_order both refuse without one, so fail early with a clear reason.
    sl_pct = bot_config.get("stop_loss_pct")
    if sl_pct is None:
        return None, "blocked: live open requires stop_loss_pct (protective stop)"
    stop, target = _sl_tp_prices(ref_price, direction, sl_pct, bot_config.get("take_profit_pct"))

    # GO-LIVE-1, bot flavor: unlike strategies (where a missing ceiling falls
    # back to the account-wide budget), a bot with no recorded ceiling is
    # BLOCKED — the ceiling is set at arming and is the operator's explicit
    # per-order bound on what this LLM-driven bot may put on.
    try:
        if not get_live_notional_ceilings().get(sid):
            return None, "blocked: no go-live notional ceiling recorded for this bot"
    except Exception as exc:
        return None, f"blocked: cannot verify go-live ceiling ({exc})"

    # WALLET ROUTING. A bot armed with a dedicated named wallet sends EVERY
    # live order there (capital isolation from the strategy pipeline): both
    # directions live in that one sub-account, so direction-book routing,
    # long-only mode, and the cross-book guard don't apply. Without a wallet,
    # mirror the kernel live path's direction-book routing exactly.
    wallet_label = str(bot_config.get("live_wallet") or "").strip().lower() or None
    books_on = False
    open_book = None
    wallet_equity: float | None = None
    if wallet_label:
        try:
            wallet_addr = books.book_address(wallet_label)
        except Exception as exc:
            return None, f"blocked: wallet registry unavailable ({exc})"
        if not wallet_addr:
            return None, (
                f"blocked: wallet {wallet_label!r} is not registered — refusing to "
                "route a real order to the master wallet (fail closed)"
            )
        open_book = wallet_label
        # A dedicated wallet must budget against ITS OWN funded balance —
        # never the master's. Unreadable balance = no order.
        wallet_equity = _book_account_equity(wallet_addr)
        if not wallet_equity or wallet_equity <= 0:
            return None, (
                f"blocked: wallet {wallet_label!r} balance unavailable or zero — "
                "fund the sub-account (fail closed)"
            )
    else:
        # DIRECTION-BOOKS routing, mirroring the kernel live path: long-only
        # skips shorts, and the M7 cross-book guard defers a crossable entry.
        try:
            books_on = books.books_enabled()
        except Exception:
            books_on = False
        if books_on:
            open_book, skip_reason = books.resolve_open_book(direction)
            if open_book is None:
                return None, f"blocked: {skip_reason or 'long-only mode (no short book)'}"
            try:
                if books.short_book_available():
                    crosses, cross_reason = _opposite_book_would_cross(coin, open_book)
                    if crosses:
                        return None, f"blocked: {cross_reason}"
            except Exception as exc:
                return None, f"blocked: cross-book guard unavailable ({exc})"

    allowed, _alloc_risk, why = can_open(
        coin, direction, sid, execution_type="live", book=open_book, enforce_risk_caps=False
    )
    if not allowed:
        return None, f"blocked: {why}"

    equity = _get_real_account_equity()
    if not equity or equity <= 0:
        return None, "blocked: real account equity unavailable (fail closed)"

    add_notional = qty * ref_price
    add_risk = abs(ref_price - float(stop)) * qty
    book_equity = wallet_equity
    if book_equity is None and books_on and open_book:
        try:
            addr = books.book_address(open_book)
            book_equity = _book_account_equity(addr) if addr else None
        except Exception:
            book_equity = None
    pb_ok, pb_why = check_live_portfolio_budget(
        coin,
        direction,
        add_risk_usd=add_risk,
        add_notional_usd=add_notional,
        equity=equity,
        book=open_book,
        book_equity_usd=book_equity,
    )
    if not pb_ok:
        return None, f"blocked: {pb_why}"
    cl_ok, cl_why = check_live_strategy_ceiling(sid, add_notional)
    if not cl_ok:
        return None, f"blocked: {cl_why}"

    risk_pct = min(add_risk / equity, 1.0)
    try:
        trade_id = execute_bot_trade(
            bot_id=bot_id,
            ticker=ticker,
            direction=direction,
            qty=qty,
            price=ref_price,
            signal_price=ref_price,
            stop_loss_price=stop,
            take_profit_price=target,
            reasoning=reasoning,
            execution_type="live",
            book=open_book,
            risk_pct=risk_pct,
            leverage=1.0,
            asset=coin,
        )
    except sqlite3.IntegrityError:
        return None, f"blocked: an OPEN {coin} {direction} already exists for this bot"
    try:
        register(
            trade_id, coin, direction, sid, risk_pct, ref_price,
            execution_type="live", book=open_book,
        )
    except Exception:
        pass

    # LIVE-1: a failed real open must not leave a phantom OPEN holding a risk
    # slot — _report_execution_failure marks it FAILED and frees the slot.
    try:
        _execute_direct(
            "open", trade_id, sid, coin, direction, qty, ref_price,
            stop_loss=stop, take_profit=target, leverage=1.0,
        )
    except Exception as exc:
        logger.error("Bot %s live open failed for %s trade=%s: %s", bot_id, coin, trade_id, exc)
        _report_execution_failure(sid, "open", trade_id, str(exc))
        return None, f"live open FAILED — {exc}"

    # _execute_direct persisted the real fill (avgPx / filled size) onto the row.
    with get_db() as conn:
        row = conn.execute(
            "SELECT fill_entry_price, entry_price, size FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
    fill_price = float((row["fill_entry_price"] if row else None) or (row["entry_price"] if row else None) or ref_price)
    filled_qty = float((row["size"] if row else None) or qty)
    position = {
        "trade_id": trade_id,
        "ticker": ticker,
        "direction": direction,
        "qty": filled_qty,
        "entry_price": fill_price,
        "current_price": fill_price,
        "stop_loss_price": stop,
        "take_profit_price": target,
        "entry_fee_usd": 0.0,  # real fees are the exchange's; folded into net at close
        "execution_type": "live",
    }
    return position, f"LIVE-OPEN {coin} {direction} x{filled_qty}"


def close_live(bot_config: dict, *, position: dict, reason: str) -> dict:
    """Close a bot's REAL position with a reduce-only order, kernel-close style.

    Returns {"state": "closed"|"pending"|"failed"|"noop", ...}; on "closed"
    includes fill_price / net_pnl / realized (the bot's post-close ledger
    total, credited by the close choke-point).
    """
    from forven.db import get_bot_equity_state, get_db
    from forven.exchange.risk import release
    from forven.scanner import (
        _close_trade_db,
        _execute_direct,
        _report_execution_failure,
        _resolve_trade_vault_address,
        _retire_trade_protection_orders,
        _trade_pending_close_reconcile,
        _trade_stop_oids,
    )
    from forven.trade_state import parse_trade_signal_data

    bot_id = str(bot_config.get("id") or "")
    sid = f"bot:{bot_id}"
    trade_id = str(position.get("trade_id") or "")
    if not trade_id:
        return {"state": "noop", "message": "no trade_id on position"}

    with get_db() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if not row:
        return {"state": "noop", "message": f"trade {trade_id} not found"}
    trade = dict(row)
    if str(trade.get("status") or "").strip().upper() != "OPEN":
        return {"state": "noop", "message": f"trade {trade_id} already closed"}
    sd = parse_trade_signal_data(trade.get("signal_data"))
    if sd.get("manual_pause"):
        return {"state": "noop", "message": "position is manually paused (operator-owned)"}
    if _trade_pending_close_reconcile(trade):
        return {"state": "pending", "message": "close already pending exchange reconcile"}

    coin = str(trade.get("asset") or "")
    direction = "short" if str(trade.get("direction") or "long").strip().lower() == "short" else "long"
    size = abs(float(trade.get("size") or 0.0))
    entry_price = float(
        trade.get("fill_entry_price") or trade.get("entry_price") or trade.get("signal_entry_price") or 0.0
    )
    ref_price = float(position.get("current_price") or 0.0) or entry_price
    if size <= 0:
        return {"state": "noop", "message": "trade has no size"}

    try:
        result = _execute_direct(
            "close", trade_id, sid, coin, direction, size, ref_price, close_reason=reason
        )
    except Exception as exc:
        logger.error("Bot %s live close failed for %s trade=%s: %s", bot_id, coin, trade_id, exc)
        _report_execution_failure(sid, "close", trade_id, str(exc))
        return {"state": "failed", "message": str(exc)}
    state = (
        str(result.get("_close_reconcile_state") or "").strip().lower()
        if isinstance(result, dict)
        else ""
    )
    if state in ("pending", "partial"):
        return {"state": "pending", "message": f"close {state} — reconcile sweep will finalize"}

    # Post-mortem fallbacks — close_trade_record prefers the recorded fill.
    signed = 1.0 if direction != "short" else -1.0
    pnl_pct = ((ref_price - entry_price) / entry_price) * signed if entry_price else 0.0
    pnl_usd = (ref_price - entry_price) * size * signed
    _close_trade_db(trade_id, ref_price, pnl_pct, pnl_usd, close_reason=reason)
    try:
        vault = _resolve_trade_vault_address(trade_id)
        stop_oids = _trade_stop_oids(trade)
        if vault:
            _retire_trade_protection_orders(coin, vault, stop_oids=stop_oids)
        else:
            _retire_trade_protection_orders(coin, stop_oids=stop_oids)
    except Exception as exc:
        logger.warning("Bot %s live close: protection-order retire failed: %s", bot_id, exc)
    try:
        release(trade_id)
    except Exception:
        pass

    with get_db() as conn:
        closed_row = conn.execute(
            "SELECT fill_exit_price, exit_price, pnl_usd FROM trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
    fill = float(
        (closed_row["fill_exit_price"] if closed_row else None)
        or (closed_row["exit_price"] if closed_row else None)
        or ref_price
    )
    net_pnl = float((closed_row["pnl_usd"] if closed_row else None) or 0.0)
    equity_state = None
    try:
        equity_state = get_bot_equity_state(bot_id)
    except Exception:
        pass
    realized = (
        float(equity_state["realized_pnl"])
        if equity_state and equity_state.get("realized_pnl") is not None
        else None
    )
    return {
        "state": "closed",
        "fill_price": fill,
        "net_pnl": net_pnl,
        "realized": realized,
    }


def flatten_live_positions(bot_config: dict, *, reason: str) -> list[dict]:
    """Close every OPEN live position this bot holds (drawdown-breach flatten).

    Best-effort per position — one failed close must not strand the rest. A
    "pending" close is left for the reconcile sweep; the caller pauses the bot
    either way, and the resting exchange stop still protects the position.
    """
    from forven.db import get_open_bot_positions

    bot_id = str(bot_config.get("id") or "")
    results: list[dict] = []
    try:
        positions = get_open_bot_positions(bot_id)
    except Exception as exc:
        return [{"state": "failed", "message": f"could not list positions: {exc}"}]
    for pos in positions:
        if str(pos.get("execution_type") or "paper").lower() != "live":
            continue
        try:
            out = close_live(bot_config, position=pos, reason=reason)
        except Exception as exc:  # a single bad row must not abort the flatten
            out = {"state": "failed", "message": str(exc)}
        out["trade_id"] = pos.get("trade_id")
        results.append(out)
    return results
