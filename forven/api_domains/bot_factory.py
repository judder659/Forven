"""API domain layer for Bot Factory — delegates to db and manager."""

from __future__ import annotations

import json

from forven.db import (
    clone_bot,
    create_bot,
    delete_bot,
    get_bot,
    get_bot_config_versions,
    get_bot_decisions,
    get_bot_equity_state,
    get_bot_trades,
    get_open_bot_positions,
    list_bots,
    update_bot,
    create_bot_template,
    delete_bot_template,
    get_bot_template,
    list_bot_templates,
)


def _live_wallet_equity(bot: dict) -> float | None:
    """Real equity of the wallet a LIVE bot routes to, from the daemon's cached
    per-wallet snapshot (no exchange round-trip). None for paper bots or when
    the snapshot lacks the wallet. A named wallet reads its own book equity; a
    bot with no named wallet reads the aggregate live account value."""
    if str(bot.get("execution_mode") or "paper").strip().lower() != "live":
        return None
    from forven.db import kv_get

    daemon_state = kv_get("daemon_state", {}) or {}
    account = daemon_state.get("exchange_account") if isinstance(daemon_state, dict) else None
    if not isinstance(account, dict):
        return None
    wallet_label = str(bot.get("live_wallet") or "").strip().lower()
    if wallet_label:
        books_map = account.get("books")
        if isinstance(books_map, dict) and wallet_label in books_map:
            try:
                return float(books_map[wallet_label])
            except (TypeError, ValueError):
                return None
        return None
    # No named wallet → master / direction-books routing: the aggregate live
    # account value is the relevant balance.
    try:
        return float(account.get("accountValue"))
    except (TypeError, ValueError):
        return None


def api_list_bots() -> list[dict]:
    bots = list_bots()
    for bot in bots:
        bot["live_wallet_equity"] = _live_wallet_equity(bot)
    return bots


def api_get_bot(bot_id: str) -> dict | None:
    return get_bot(bot_id)


def api_create_bot(config: dict) -> dict:
    bot_id = create_bot(config)
    return get_bot(bot_id)


def api_update_bot(bot_id: str, updates: dict) -> dict | None:
    update_bot(bot_id, updates)
    return get_bot(bot_id)


def api_delete_bot(bot_id: str) -> dict:
    # A bot holding OPEN live positions must not be deleted — its trades mirror
    # REAL exchange positions, and dropping the config would orphan them with
    # no owner to close them. Close them first (stop the bot / manual close).
    if _open_live_trade_count(bot_id) > 0:
        raise ValueError(
            "Bot has open LIVE positions — close them before deleting the bot."
        )
    # Stop the bot first so we don't delete config out from under a live
    # subprocess that's still writing trades.
    try:
        from forven.bot_factory.manager import BotManager

        BotManager.get_instance().stop_bot(bot_id)
    except Exception:
        pass
    # Close any OPEN paper trades so they don't linger as phantom exposure once
    # the config (and thus attribution) is gone.
    try:
        from forven.db import close_open_bot_trades

        close_open_bot_trades(bot_id, reason="bot_deleted")
    except Exception:
        pass
    # Drop the bot's isolated ChromaDB memory collection (unbounded leak otherwise).
    try:
        from forven.bot_factory.memory import BotMemory

        BotMemory(bot_id).delete_collection()
    except Exception:
        pass
    delete_bot(bot_id)
    return {"status": "deleted", "bot_id": bot_id}


def api_clone_bot(bot_id: str, new_name: str) -> dict:
    new_id = clone_bot(bot_id, new_name)
    return get_bot(new_id)


def api_start_bot(bot_id: str) -> dict:
    from forven.bot_factory.manager import BotManager
    manager = BotManager.get_instance()
    return manager.start_bot(bot_id)


def api_stop_bot(bot_id: str) -> dict:
    from forven.bot_factory.manager import BotManager
    manager = BotManager.get_instance()
    return manager.stop_bot(bot_id)


def api_kill_all() -> dict:
    from forven.bot_factory.manager import BotManager
    manager = BotManager.get_instance()
    return manager.kill_all()


def api_get_decisions(bot_id: str, limit: int = 50) -> list[dict]:
    return get_bot_decisions(bot_id, limit=limit)


def api_get_trades(bot_id: str, limit: int = 50) -> list[dict]:
    return get_bot_trades(bot_id, limit=limit)


def api_get_stats(bot_id: str) -> dict:
    """Aggregate trade stats over ALL of a bot's trades (server-side, uncapped)."""
    from forven.db import get_bot_trade_stats

    return get_bot_trade_stats(bot_id)


def api_get_positions(bot_id: str) -> dict:
    """Return open positions (with SL/TP levels) plus equity state so the UI
    can render a live snapshot matching what the runner sees in memory.

    Marks come from the daemon's price snapshot (the same KV feed the rest of
    the app trades off) so the UI shows live unrealized P&L without opening
    its own exchange sockets."""
    positions = get_open_bot_positions(bot_id)
    equity = get_bot_equity_state(bot_id) or {}
    bot = get_bot(bot_id) or {}
    starting_capital = float(bot.get("capital_allocation") or 0)
    realized_pnl = float(equity.get("realized_pnl") or 0)
    peak_equity = equity.get("peak_equity")

    mark_age_seconds = None
    unrealized_pnl = 0.0
    try:
        from forven.market_cache import load_price_snapshot

        prices, mark_age_seconds = load_price_snapshot()
        for pos in positions:
            coin = str(pos.get("asset") or pos.get("ticker") or "").strip().upper()
            coin = coin.split("/", 1)[0] if "/" in coin else coin
            mark = prices.get(coin)
            if mark is None or mark <= 0:
                continue
            pos["current_price"] = float(mark)
            entry = float(pos.get("entry_price") or 0)
            qty = float(pos.get("qty") or 0)
            if entry and qty:
                signed = 1.0 if str(pos.get("direction") or "long") != "short" else -1.0
                pnl = (float(mark) - entry) * qty * signed - float(pos.get("entry_fee_usd") or 0)
                pos["unrealized_pnl"] = round(pnl, 4)
                unrealized_pnl += pnl
    except Exception:
        pass  # no snapshot (daemon down) → positions serve with current_price=None

    live_wallet_equity = _live_wallet_equity(bot)
    return {
        "bot_id": bot_id,
        "starting_capital": starting_capital,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": round(unrealized_pnl, 4),
        "equity": round(starting_capital + realized_pnl + unrealized_pnl, 4),
        "peak_equity": float(peak_equity) if peak_equity is not None else None,
        "equity_state_started_at": equity.get("equity_state_started_at"),
        "mark_age_seconds": mark_age_seconds,
        "open_positions": positions,
        "execution_mode": str(bot.get("execution_mode") or "paper"),
        # Real balance of the wallet this LIVE bot trades (None for paper).
        # The UI shows this as equity for live bots instead of paper capital.
        "live_wallet": bot.get("live_wallet"),
        "live_wallet_equity": live_wallet_equity,
    }


def api_get_versions(bot_id: str) -> list[dict]:
    return get_bot_config_versions(bot_id)


def api_get_memory(bot_id: str, limit: int = 50) -> list[dict]:
    """Return recent entries from this bot's isolated ChromaDB collection."""
    from forven.bot_factory.memory import BotMemory
    return BotMemory(bot_id).list_recent(limit=limit)


def api_diff_versions(bot_id: str, v1: int, v2: int) -> dict:
    """Diff two config versions field-by-field."""
    versions = get_bot_config_versions(bot_id)
    version_map = {v["version"]: v.get("config_snapshot", {}) for v in versions}

    snap1 = version_map.get(v1)
    snap2 = version_map.get(v2)

    if snap1 is None or snap2 is None:
        return {"error": "Version not found", "available": list(version_map.keys())}

    if isinstance(snap1, str):
        snap1 = json.loads(snap1)
    if isinstance(snap2, str):
        snap2 = json.loads(snap2)

    diff = {}
    all_keys = set(list(snap1.keys()) + list(snap2.keys()))
    for key in sorted(all_keys):
        val1 = snap1.get(key)
        val2 = snap2.get(key)
        if val1 != val2:
            diff[key] = {"v1": val1, "v2": val2}

    return {"v1": v1, "v2": v2, "changes": diff}


# ── Templates ────────────────────────────────────────────────────────


def api_list_templates() -> list[dict]:
    return list_bot_templates()


def api_get_template(template_id: str) -> dict | None:
    return get_bot_template(template_id)


def api_create_template(name: str, description: str | None, config: dict) -> dict:
    template_id = create_bot_template(name, description, config)
    return get_bot_template(template_id)


def api_delete_template(template_id: str) -> dict:
    delete_bot_template(template_id)
    return {"status": "deleted", "template_id": template_id}


def api_close_position(bot_id: str, trade_id: str, reason: str | None = None) -> dict:
    """Manually close one of a bot's open positions (operator control).

    Dispatches on the position's execution type: a live row gets a REAL
    reduce-only close through the sanctioned path (bot_factory.live_exec); a
    paper row closes at the daemon's current mark with the bot's modeled
    slippage + fees (matching the runner's own close semantics). The close
    choke-point credits the bot's realized-P&L ledger either way.
    """
    from forven.db import close_bot_trade

    bot = get_bot(bot_id)
    if not bot:
        raise ValueError(f"Bot {bot_id} not found")
    positions = get_open_bot_positions(bot_id)
    pos = next((p for p in positions if str(p.get("trade_id")) == str(trade_id)), None)
    if not pos:
        raise ValueError(f"No open position {trade_id} for this bot")

    from forven.market_cache import load_price_snapshot

    prices, _age = load_price_snapshot()
    coin = str(pos.get("asset") or pos.get("ticker") or "").strip().upper()
    coin = coin.split("/", 1)[0] if "/" in coin else coin
    mark = prices.get(coin)

    if str(pos.get("execution_type") or "paper").lower() == "live":
        from forven.bot_factory.live_exec import close_live

        if mark and mark > 0:
            pos = dict(pos)
            pos["current_price"] = float(mark)
        out = close_live(bot, position=pos, reason=reason or "manual_close")
        if out.get("state") == "failed":
            raise ValueError(f"Live close failed: {out.get('message')}")
        return {"status": out.get("state"), "trade_id": trade_id, **{
            k: out.get(k) for k in ("fill_price", "net_pnl", "realized") if out.get(k) is not None
        }}

    # Paper: fail closed without a current mark — closing at a fabricated
    # price would corrupt the bot's ledger.
    if not mark or mark <= 0:
        raise ValueError(f"No current price available for {coin} — daemon snapshot missing")
    from forven.bot_factory.runner import _apply_slippage, _fee_usd

    direction = str(pos.get("direction") or "long")
    is_buy = direction == "short"  # closing a short buys back
    slippage_bps = float(bot.get("slippage_bps") or 0)
    fee_bps = float(bot.get("taker_fee_bps") or 0)
    fill = _apply_slippage(float(mark), is_buy, slippage_bps)
    exit_fee = _fee_usd(fill * float(pos.get("qty") or 0), fee_bps)
    result = close_bot_trade(
        trade_id,
        exit_price=fill,
        signal_exit_price=float(mark),
        exit_slippage_bps=slippage_bps or None,
        exit_fee_bps=fee_bps or None,
        exit_fee_usd=exit_fee or None,
        reason=reason or "manual_close",
    )
    if not result or not result.get("updated"):
        raise ValueError("Position was already closed")
    return {
        "status": "closed",
        "trade_id": trade_id,
        "fill_price": fill,
        "net_pnl": result.get("pnl_usd"),
        "realized": result.get("bot_realized_pnl"),
    }


def api_set_bot_wallet(bot_id: str, wallet: str | None) -> dict:
    """Set the wallet this bot's LIVE orders will route to (None = master/books).

    Freely editable while the bot is in PAPER mode — it just records where the
    bot will trade once armed. Refused while the bot is LIVE-armed: silently
    redirecting a live bot's order routing must go through the GO LIVE
    re-arming flow (which preselects this stored wallet).
    """
    from forven.db import set_bot_live_wallet
    from forven.exchange import books

    bot = get_bot(bot_id)
    if not bot:
        raise ValueError(f"Bot {bot_id} not found")
    if str(bot.get("execution_mode") or "paper").strip().lower() == "live":
        raise ValueError(
            "Bot is LIVE-armed — switch it to paper (or re-arm via GO LIVE) to change its wallet."
        )
    wallet_label = str(wallet or "").strip().lower() or None
    if wallet_label is not None and wallet_label not in books.named_wallets():
        raise ValueError(
            f"wallet {wallet_label!r} is not a registered named wallet — "
            "register it under Settings › HyperLiquid first"
        )
    set_bot_live_wallet(bot_id, wallet_label)
    return get_bot(bot_id)


# ── Execution mode (paper / live) ───────────────────────────────────


def _open_live_trade_count(bot_id: str) -> int:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE source = ? AND status = 'OPEN' "
            "AND LOWER(COALESCE(execution_type, 'paper')) = 'live'",
            (f"bot:{bot_id}",),
        ).fetchone()
        return int(row["n"] or 0) if row else 0


def api_go_live(bot_id: str, confirm: str | None, ceiling_usd, wallet: str | None = None) -> dict:
    """Arm a bot for LIVE execution — the Bot Factory mirror of GO-LIVE-1.

    Requires the same typed "GO LIVE" confirmation + per-bot notional ceiling
    the strategy pipeline demands, a configured stop_loss_pct (every live
    order must carry a protective stop), and a stopped bot (the runner reads
    its mode at spawn — no mid-flight flips).

    `wallet` optionally routes ALL of this bot's live orders to a registered
    named sub-account wallet, isolating its capital from the strategy
    pipeline's wallet. None = legacy routing (direction books / master).
    """
    from forven.db import log_activity, set_bot_execution_mode, set_bot_live_wallet
    from forven.exchange import books
    from forven.exchange.risk import (
        set_live_notional_ceiling,
        validate_go_live_confirmation,
    )

    bot = get_bot(bot_id)
    if not bot:
        raise ValueError(f"Bot {bot_id} not found")
    status = bot.get("runtime_status") or bot.get("status") or "stopped"
    if status == "running":
        raise ValueError("Stop the bot before changing its execution mode.")
    if bot.get("stop_loss_pct") is None:
        raise ValueError(
            "Live mode requires stop_loss_pct — every live order carries a "
            "protective stop resting on the exchange."
        )
    wallet_label = str(wallet or "").strip().lower() or None
    if wallet_label is not None and wallet_label not in books.named_wallets():
        raise ValueError(
            f"wallet {wallet_label!r} is not a registered named wallet — "
            "register it under Wallets first"
        )
    error = validate_go_live_confirmation(confirm, ceiling_usd)
    if error:
        raise ValueError(error)

    set_live_notional_ceiling(
        f"bot:{bot_id}", float(ceiling_usd), actor="bot_factory_go_live"
    )
    set_bot_live_wallet(bot_id, wallet_label)
    set_bot_execution_mode(bot_id, "live")
    log_activity(
        "warning", "bot_factory",
        f"Bot '{bot.get('name', bot_id)}' armed for LIVE execution "
        f"(per-order notional ceiling ${float(ceiling_usd):,.0f}, "
        f"wallet {wallet_label or 'master/books'})",
        {"bot_id": bot_id, "ceiling_usd": float(ceiling_usd), "wallet": wallet_label},
    )
    return get_bot(bot_id)


def api_go_paper(bot_id: str) -> dict:
    """Disarm live execution and return the bot to paper mode.

    Refused while the bot is running or still holds OPEN live positions —
    a paper-mode runner would stop managing them while they sit as real
    exchange exposure.
    """
    from forven.db import log_activity, set_bot_execution_mode
    from forven.exchange.risk import set_live_notional_ceiling

    bot = get_bot(bot_id)
    if not bot:
        raise ValueError(f"Bot {bot_id} not found")
    status = bot.get("runtime_status") or bot.get("status") or "stopped"
    if status == "running":
        raise ValueError("Stop the bot before changing its execution mode.")
    if _open_live_trade_count(bot_id) > 0:
        raise ValueError(
            "Bot has open LIVE positions — close them before switching to paper."
        )

    set_bot_execution_mode(bot_id, "paper")
    try:
        set_live_notional_ceiling(f"bot:{bot_id}", None, actor="bot_factory_go_paper")
    except Exception:
        pass  # ceiling is only consulted on live opens, which paper mode never makes
    log_activity(
        "info", "bot_factory",
        f"Bot '{bot.get('name', bot_id)}' returned to PAPER execution",
        {"bot_id": bot_id},
    )
    return get_bot(bot_id)


# ── Strategy-to-Bot Bridge ──────────────────────────────────────────


def api_create_bot_from_strategy(strategy_id: str) -> dict:
    """Create a bot config pre-filled from a strategy container."""
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT id, name, type, symbol, timeframe, params, metrics FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Strategy {strategy_id} not found")

        strategy = dict(row)

    params = strategy.get("params")
    if isinstance(params, str):
        import json as _json
        try:
            params = _json.loads(params)
        except Exception:
            params = {}

    # strategies.symbol is a bare base ("BTC", "SOL"); the runner's market fetch
    # needs a full pair ("BTC/USDT"), so normalize before locking (BFAPI-8).
    raw_symbol = strategy.get("symbol")
    pair = None
    if raw_symbol:
        pair = raw_symbol if "/" in raw_symbol else f"{raw_symbol}/USDT"

    orig_tf = strategy.get("timeframe", "1h")
    config = {
        "name": f"Bot from {strategy.get('name', strategy_id)}",
        # No hardcoded model — inherits the operator's configured default at create.
        "context": (
            f"This bot is seeded from strategy {strategy_id} ({strategy.get('name', '')}).\n"
            f"Type: {strategy.get('type', 'unknown')}\n"
            f"Symbol: {pair or 'unknown'}\n"
            f"Original strategy timeframe: {orig_tf} — NOTE: this bot observes 1-hour candles.\n"
            f"Parameters: {params}"
        ),
        "strategy": (
            f"Trade a {strategy.get('type', 'unknown')} strategy on {pair or 'the locked pair'} "
            f"using the 1-hour candles you are given. Use the parameters and rules above as guidance, "
            f"adapting them to the 1-hour timeframe."
        ),
        "asset_mode": "locked",
        "locked_pairs": [pair] if pair else None,
    }

    return {"config": config, "strategy_id": strategy_id}
