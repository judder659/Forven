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


def _with_model_check(bot: dict | None) -> dict | None:
    """Attach whether the bot's model is callable here, so the UI can flag a
    retired or disconnected model before Start does."""
    if bot is None:
        return None
    from forven.bot_factory.manager import check_bot_model
    from forven.bot_factory.rules import needs_ai

    bot["model_check"] = (
        check_bot_model(bot.get("model")) if needs_ai(bot)
        else {"ok": True, "reason": None, "not_needed": True}
    )
    return bot


def api_list_bots() -> list[dict]:
    bots = list_bots()
    for bot in bots:
        bot["live_wallet_equity"] = _live_wallet_equity(bot)
        _with_model_check(bot)
    return bots


def api_get_bot(bot_id: str) -> dict | None:
    return _with_model_check(get_bot(bot_id))


def _check_rule_settings(config: dict) -> None:
    from forven.bot_factory.rules import validate_rule_params

    if "bot_type" in config or "rule_params" in config:
        problem = validate_rule_params(config.get("bot_type"), config.get("rule_params"))
        if problem:
            raise ValueError(problem)


def api_create_bot(config: dict) -> dict:
    _check_rule_settings(config)
    bot_id = create_bot(config)
    return _with_model_check(get_bot(bot_id))


def api_update_bot(bot_id: str, updates: dict) -> dict | None:
    # 0 clears the daily dollar cap (an update can't send None to mean "clear").
    if updates.get("max_llm_cost_per_day") == 0:
        updates["max_llm_cost_per_day"] = None
    if "bot_type" in updates or "rule_params" in updates:
        current = get_bot(bot_id) or {}
        _check_rule_settings({
            "bot_type": updates.get("bot_type", current.get("bot_type")),
            "rule_params": updates.get("rule_params", current.get("rule_params")),
        })
    update_bot(bot_id, updates)
    return _with_model_check(get_bot(bot_id))


def api_use_default_model(bot_id: str) -> dict:
    """Switch a bot to the operator's primary model (the Start-refusal fix)."""
    from forven.bot_factory.manager import default_model_key

    if not get_bot(bot_id):
        raise LookupError(f"Bot {bot_id} not found")
    key = default_model_key()
    if not key:
        raise ValueError("No default model is configured. Set one under Agents → Routing.")
    update_bot(bot_id, {"model": key})
    return _with_model_check(get_bot(bot_id))


def _watched_pair_count(asset_mode: str | None, locked_pairs: object) -> int:
    from forven.bot_factory.ai_costs import FREE_ROAM_PAIRS

    if asset_mode == "locked" and isinstance(locked_pairs, list) and locked_pairs:
        return len(locked_pairs)
    return FREE_ROAM_PAIRS


def api_cost_estimate(
    *,
    model: str | None,
    decision_cadence: str,
    cooldown_seconds: int,
    timeframe: str = "1h",
    asset_mode: str | None,
    locked_pairs: list[str] | None,
    reasoning_verbosity: str,
    max_llm_calls_per_day: int | None,
    max_llm_cost_per_day: float | None,
    bot_id: str | None = None,
    data_sources: list[str] | None = None,
) -> dict:
    """Projected AI calls and spend per day for an editor configuration. With
    a bot id, uses that bot's recorded tokens per call when it has any."""
    from forven.bot_factory.ai_costs import project_daily_spend
    from forven.bot_factory.inputs import normalize_inputs
    from forven.bot_factory.manager import check_bot_model, resolve_bot_route
    from forven.db import (
        BOT_TIMEFRAME_MINUTES,
        get_bot_ai_usage,
        normalize_bot_cadence,
        normalize_bot_timeframe,
    )

    avg_in = avg_out = None
    if bot_id:
        usage = get_bot_ai_usage(bot_id)
        if usage["sampled_calls"] >= 5:
            avg_in, avg_out = usage["avg_tokens_in"], usage["avg_tokens_out"]
    try:
        provider, model_id = resolve_bot_route(model)
    except Exception:
        provider, model_id = None, str(model or "")
    tf = normalize_bot_timeframe(timeframe)
    projection = project_daily_spend(
        provider=provider,
        model_id=model_id,
        cadence=normalize_bot_cadence(decision_cadence),
        cooldown_seconds=cooldown_seconds,
        timeframe=tf,
        bar_minutes=BOT_TIMEFRAME_MINUTES[tf],
        pairs=_watched_pair_count(asset_mode, locked_pairs),
        verbosity=reasoning_verbosity,
        max_llm_calls_per_day=max_llm_calls_per_day,
        max_llm_cost_per_day=max_llm_cost_per_day,
        avg_tokens_in=avg_in,
        avg_tokens_out=avg_out,
        inputs=normalize_inputs(data_sources),
    )
    projection["model_check"] = check_bot_model(model)
    return projection


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
    """Aggregate trade stats over ALL of a bot's trades (server-side, uncapped),
    plus its estimated AI spend to date."""
    from forven.db import get_bot_ai_usage, get_bot_trade_stats

    stats = get_bot_trade_stats(bot_id)
    usage = get_bot_ai_usage(bot_id)
    stats["ai_cost_usd"] = usage["total_cost_usd"]
    stats["ai_priced_calls"] = usage["priced_calls"]
    return stats


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


def api_live_gate(bot_id: str) -> dict:
    """What the bot has shown against the live gate, check by check."""
    from forven.bot_factory import live_gate
    from forven.db import get_bot_trade_ledger, list_bot_replays

    bot = get_bot(bot_id)
    if not bot:
        raise LookupError(f"Bot {bot_id} not found")
    versions = get_bot_config_versions(bot_id)
    result = live_gate.evaluate(
        ledger=get_bot_trade_ledger(bot_id),
        replays=list_bot_replays(bot_id=bot_id, limit=20),
        settings_changed_at=versions[0]["created_at"] if versions else None,
        now=_now_utc(),
    )
    result["bot_id"] = bot_id
    return result


def live_gate_refusal(bot_id: str) -> str | None:
    from forven.bot_factory.live_gate import refusal

    return refusal(api_live_gate(bot_id))


def api_go_live(bot_id: str, confirm: str | None, ceiling_usd, wallet: str | None = None) -> dict:
    """Arm a bot for LIVE execution — the Bot Factory mirror of GO-LIVE-1.

    Requires a passed live gate (``forven.bot_factory.live_gate``), the
    same typed "GO LIVE" confirmation + per-bot notional ceiling
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
    from forven.bot_factory.rules import is_rule_bot

    if is_rule_bot(bot):
        # Grid and DCA exits are per-lot targets the live order path doesn't
        # place yet; rule bots stay on paper until they have a live gate.
        raise ValueError("Grid, DCA and Signal-follower bots run on paper only for now.")
    gate_problem = live_gate_refusal(bot_id)
    if gate_problem:
        raise ValueError(gate_problem)
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

    from forven.db import BOT_TIMEFRAME_MINUTES

    orig_tf = str(strategy.get("timeframe") or "1h").strip().lower()
    # The bot reads the strategy's own candles when it's a bot timeframe;
    # otherwise the nearest it can (1h) and says so.
    bot_tf = orig_tf if orig_tf in BOT_TIMEFRAME_MINUTES else "1h"
    tf_note = (
        f"Strategy timeframe: {orig_tf}; this bot reads the same {bot_tf} candles."
        if bot_tf == orig_tf
        else f"Original strategy timeframe: {orig_tf} — NOTE: this bot observes {bot_tf} candles."
    )
    config = {
        "name": f"Bot from {strategy.get('name', strategy_id)}",
        # No hardcoded model — inherits the operator's configured default at create.
        "context": (
            f"This bot is seeded from strategy {strategy_id} ({strategy.get('name', '')}).\n"
            f"Type: {strategy.get('type', 'unknown')}\n"
            f"Symbol: {pair or 'unknown'}\n"
            f"{tf_note}\n"
            f"Parameters: {params}"
        ),
        "strategy": (
            f"Trade a {strategy.get('type', 'unknown')} strategy on {pair or 'the locked pair'} "
            f"using the {bot_tf} candles you are given. Use the parameters and rules above as guidance"
            + ("." if bot_tf == orig_tf else f", adapting them to the {bot_tf} timeframe.")
        ),
        "timeframe": bot_tf,
        "asset_mode": "locked",
        "locked_pairs": [pair] if pair else None,
    }

    return {"config": config, "strategy_id": strategy_id}


def api_bot_credential(bot_id: str, token: str | None) -> dict:
    """The credential broker behind a bot subprocess's provider login.

    Raises PermissionError for a bad token, LookupError for an unknown bot,
    ValueError when the bot's provider has no usable credential.
    """
    from forven.bot_factory.manager import (
        bot_process_is_live,
        brokered_credential_for_bot,
        verify_bot_token,
    )

    provider = verify_bot_token(bot_id, token)
    if not provider or not bot_process_is_live(bot_id):
        raise PermissionError("invalid bot credential token")
    return brokered_credential_for_bot(bot_id, provider)


def _bot_owns_trade(bot_id: str, trade_id: object) -> bool:
    from forven.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT source, execution_type FROM trades WHERE id = ?", (str(trade_id or ""),)
        ).fetchone()
    return bool(row) and row["source"] == f"bot:{bot_id}" and (
        str(row["execution_type"] or "paper").lower() == "paper"
    )


def api_bot_write(bot_id: str, token: str | None, op: str, args: dict | None) -> dict:
    """One database write from a bot subprocess, on its own rows only.

    Raises PermissionError for a bad token or a row that isn't this bot's,
    ValueError for an unknown write or bad arguments.
    """
    import forven.db as db
    from forven.bot_factory.bot_writes import OPS
    from forven.bot_factory.manager import bot_process_is_live, verify_bot_token

    if not verify_bot_token(bot_id, token) or not bot_process_is_live(bot_id):
        raise PermissionError("invalid bot token")
    if op not in OPS:
        raise ValueError(f"unknown bot write {op!r}")
    args = dict(args or {})
    if "bot_id" in args and args["bot_id"] != bot_id:
        raise PermissionError("a bot may only write its own rows")
    if op == "close_bot_trade" and not _bot_owns_trade(bot_id, args.get("trade_id")):
        raise PermissionError("not this bot's paper trade")
    if op == "execute_bot_trade":
        if str(args.get("execution_type") or "paper").lower() != "paper" or args.get("book"):
            raise PermissionError("live orders are not recorded through this route")
    if op == "log_activity":
        data = args.get("data")
        if args.get("source") != "bot_factory" or not isinstance(data, dict) or data.get("bot_id") != bot_id:
            raise PermissionError("bots may only log their own bot_factory activity")
    try:
        result = getattr(db, op)(**args)
    except TypeError as e:
        raise ValueError(f"bad arguments for {op}: {e}") from e
    return {"result": result}


def api_bot_read(bot_id: str, token: str | None, op: str, args: dict | None) -> dict:
    """One read from a bot subprocess: its own rows, or market data.

    Raises PermissionError for a bad token or another bot's rows, ValueError
    for an unknown read or bad arguments.
    """
    import importlib

    from forven.bot_factory.bot_writes import READ_OPS
    from forven.bot_factory.manager import bot_process_is_live, verify_bot_token

    if not verify_bot_token(bot_id, token) or not bot_process_is_live(bot_id):
        raise PermissionError("invalid bot token")
    if op not in READ_OPS:
        raise ValueError(f"unknown bot read {op!r}")
    args = dict(args or {})
    if "bot_id" in args and args["bot_id"] != bot_id:
        raise PermissionError("a bot may only read its own rows")
    if op == "get_open_followed_trades":
        from forven.db import get_bot

        followed = (((get_bot(bot_id) or {}).get("rule_params")) or {}).get("strategy_id")
        if not followed or args.get("strategy_id") != followed:
            raise PermissionError("a bot may only read the strategy it follows")
    fn = getattr(importlib.import_module(READ_OPS[op]), op)
    try:
        result = fn(**args)
    except TypeError as e:
        raise ValueError(f"bad arguments for {op}: {e}") from e
    return {"result": result}


_MAX_PROMPT_CHARS = 400_000


async def api_bot_ai(bot_id: str, token: str | None, body: dict | None) -> dict:
    """A bot subprocess's model call, made here with Forven's own login.

    The model must belong to the provider the bot was started with. Raises
    PermissionError for a bad token or another provider, ValueError for a
    bad request. A failed model call answers ``{"error": ...}``.
    """
    from forven.ai import normalize_provider_and_model
    from forven.bot_factory.engine import call_model_here
    from forven.bot_factory.manager import bot_process_is_live, verify_bot_token

    provider = verify_bot_token(bot_id, token)
    if not provider or not bot_process_is_live(bot_id):
        raise PermissionError("invalid bot token")
    args = dict((body or {}).get("args") or {})
    model = str(args.get("model") or "").strip()
    messages = args.get("messages")
    if not model or not isinstance(messages, list) or not messages:
        raise ValueError("model and messages are required")
    if any(
        not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant")
        or not isinstance(m.get("content"), str)
        for m in messages
    ):
        raise ValueError("messages must have a role and text content")
    if sum(len(m["content"]) for m in messages) > _MAX_PROMPT_CHARS:
        raise ValueError("prompt too long")
    if normalize_provider_and_model("auto", model)[0] != provider:
        raise PermissionError("a bot may only use the provider it was started with")
    try:
        max_tokens = min(max(int(args.get("max_tokens") or 1024), 1), 4096)
        temperature = min(max(float(args.get("temperature") or 0.0), 0.0), 2.0)
    except (TypeError, ValueError) as e:
        raise ValueError(f"bad arguments: {e}") from e
    try:
        return {"result": await call_model_here(
            model, [{"role": m["role"], "content": m["content"]} for m in messages],
            max_tokens=max_tokens, temperature=temperature,
        )}
    except Exception as e:  # the model's failure, reported to the bot as its own
        return {"result": {"error": str(e) or e.__class__.__name__}}


# ── Cockpit: performance, chart, fleet, decision inspector ───────────

BENCHMARK_SYMBOL = "BTC/USDT"
_CHART_BARS = 300


def _now_utc():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


def _coin_of(pair: object) -> str:
    raw = str(pair or "").strip().upper()
    return raw.split("/", 1)[0] if "/" in raw else raw


def _price_marks() -> dict[str, float]:
    """Latest marks by coin from the daemon's price snapshot ({} when down)."""
    try:
        from forven.market_cache import load_price_snapshot

        prices, _age = load_price_snapshot()
        return {str(k).upper(): float(v) for k, v in (prices or {}).items() if v}
    except Exception:
        return {}


def _ledger_unrealized(ledger: list[dict], marks: dict[str, float]) -> float:
    """Unrealized P&L of a ledger's open trades at the given marks, net of the
    entry fee already paid. Trades with no mark count as flat."""
    total = 0.0
    for trade in ledger:
        if trade.get("status") != "OPEN":
            continue
        mark = marks.get(_coin_of(trade.get("pair")))
        entry = float(trade.get("entry_price") or 0)
        size = float(trade.get("size") or 0)
        if not mark or not entry or not size:
            continue
        sign = -1.0 if str(trade.get("direction") or "long") == "short" else 1.0
        total += (mark - entry) * size * sign - float(trade.get("fees_usd") or 0)
    return total


def _candle_rows(pair: str, timeframe: str, *, start=None, max_rows: int = _CHART_BARS) -> list[dict]:
    """Stored closed candles as {timestamp, open, high, low, close, volume}."""
    from forven.data import load_parquet
    from forven.market_data import dataframe_to_ohlcv_rows

    try:
        df = load_parquet(pair, timeframe, start=start) if start is not None else load_parquet(pair, timeframe)
    except Exception:
        df = None
    if df is None or df.empty:
        return []
    from forven.bot_factory.runner import _indexed_by_time

    rows = dataframe_to_ohlcv_rows(_indexed_by_time(df), max_rows=max_rows)
    return [
        {"timestamp": r["t"], "open": r["open"], "high": r["high"], "low": r["low"],
         "close": r["close"], "volume": r["volume"]}
        for r in rows
    ]


def _benchmark_prices(start) -> list[tuple]:
    """Hourly benchmark closes from a little before `start`, as (time, close)."""
    from datetime import timedelta

    from forven.bot_factory.performance import parse_time

    if start is None:
        return []
    naive_start = (start - timedelta(hours=2)).replace(tzinfo=None)
    rows = _candle_rows(BENCHMARK_SYMBOL, "1h", start=naive_start, max_rows=200_000)
    return [(parse_time(r["timestamp"]), r["close"]) for r in rows]


def _bot_performance(
    bot: dict,
    *,
    ledger: list[dict],
    digest: dict | None,
    marks: dict[str, float],
    benchmark_prices: list[tuple] | None,
    ai_cost_usd: float,
    funding_accrued: float,
) -> dict:
    from forven.bot_factory.performance import (
        benchmark_curve,
        equity_curve,
        performance_start,
        summarize,
    )

    now = _now_utc()
    capital = float(bot.get("capital_allocation") or 0)
    start = performance_start(
        ledger,
        first_decision_at=(digest or {}).get("first_decision_at"),
        created_at=bot.get("created_at"),
    ) or now
    realized = sum(
        float(t["pnl_usd"]) for t in ledger if t.get("status") == "CLOSED" and t.get("pnl_usd") is not None
    ) - float(funding_accrued or 0)
    current_equity = capital + realized + _ledger_unrealized(ledger, marks)
    curve = equity_curve(capital, ledger, start_at=min(start, now), now=now, current_equity=current_equity)
    bench = benchmark_curve(benchmark_prices or [], curve, capital) if benchmark_prices else None
    summary = summarize(
        starting_capital=capital,
        ledger=ledger,
        curve=curve,
        benchmark=bench,
        ai_cost_usd=ai_cost_usd,
        benchmark_symbol=BENCHMARK_SYMBOL,
    )
    summary["start_at"] = curve[0]["timestamp"]
    summary["end_at"] = curve[-1]["timestamp"]
    summary["open_positions"] = sum(1 for t in ledger if t.get("status") == "OPEN")
    summary["funding_usd"] = round(float(funding_accrued or 0), 4)
    return {"summary": summary, "curve": curve, "benchmark_curve": bench}


def api_bot_performance(bot_id: str) -> dict:
    """Equity curve, buy-and-hold benchmark and stat tiles for one bot."""
    from forven.db import get_bot_ai_usage, get_bot_trade_ledger, get_fleet_decision_digest

    bot = get_bot(bot_id)
    if not bot:
        raise LookupError(f"Bot {bot_id} not found")
    ledger = get_bot_trade_ledger(bot_id)
    digest = get_fleet_decision_digest().get(bot_id)
    equity_state = get_bot_equity_state(bot_id) or {}
    from forven.bot_factory.performance import performance_start

    start = performance_start(
        ledger,
        first_decision_at=(digest or {}).get("first_decision_at"),
        created_at=bot.get("created_at"),
    )
    perf = _bot_performance(
        bot,
        ledger=ledger,
        digest=digest,
        marks=_price_marks(),
        benchmark_prices=_benchmark_prices(start),
        ai_cost_usd=get_bot_ai_usage(bot_id)["total_cost_usd"],
        funding_accrued=float(equity_state.get("funding_accrued") or 0),
    )
    perf["bot_id"] = bot_id
    return perf


def api_bot_chart(bot_id: str, pair: str | None = None, bars: int = _CHART_BARS) -> dict:
    """Candles of the bot's timeframe for one pair, with its entries and exits."""
    from forven.bot_factory.performance import parse_time
    from forven.db import get_bot_trade_ledger, normalize_bot_timeframe

    bot = get_bot(bot_id)
    if not bot:
        raise LookupError(f"Bot {bot_id} not found")
    timeframe = normalize_bot_timeframe(bot.get("timeframe"))
    ledger = get_bot_trade_ledger(bot_id)

    pairs: list[str] = []
    for trade in reversed(ledger):  # most recently traded first
        if trade.get("pair") and trade["pair"] not in pairs:
            pairs.append(trade["pair"])
    locked = bot.get("locked_pairs")
    if bot.get("asset_mode") == "locked" and isinstance(locked, list):
        pairs += [p for p in locked if p and p not in pairs]
    if not pairs:
        pairs = [BENCHMARK_SYMBOL]
    chosen = pair if pair else pairs[0]

    candles = _candle_rows(chosen, timeframe, max_rows=max(20, min(int(bars), 1000)))
    first = parse_time(candles[0]["timestamp"]) if candles else None
    entries: list[dict] = []
    exits: list[dict] = []
    for trade in ledger:
        if trade.get("pair") != chosen:
            continue
        opened = parse_time(trade.get("opened_at"))
        if opened and trade.get("entry_price") and (first is None or opened >= first):
            entries.append({
                "timestamp": opened.isoformat(), "price": float(trade["entry_price"]),
                "type": "entry", "direction": trade.get("direction"),
            })
        closed = parse_time(trade.get("closed_at"))
        if closed and trade.get("exit_price") and (first is None or closed >= first):
            exits.append({
                "timestamp": closed.isoformat(), "price": float(trade["exit_price"]),
                "type": "exit", "direction": trade.get("direction"), "pnl_usd": trade.get("pnl_usd"),
            })
    return {
        "bot_id": bot_id,
        "pair": chosen,
        "pairs": pairs if chosen in pairs else [chosen, *pairs],
        "timeframe": timeframe,
        "candles": candles,
        "entries": entries,
        "exits": exits,
    }


def _bot_health(bot: dict, digest: dict | None, now) -> dict:
    """A health dot: model callable, process alive, deciding on schedule, and
    candles fresh. Levels: ok, warn, bad, or idle for a healthy stopped bot."""
    from forven.bot_factory.performance import parse_time
    from forven.bot_factory.rules import is_rule_bot
    from forven.db import BOT_TIMEFRAME_MINUTES, normalize_bot_cadence, normalize_bot_timeframe

    bad: list[str] = []
    warn: list[str] = []
    status = str(bot.get("runtime_status") or bot.get("status") or "stopped")
    check = bot.get("model_check") or {}
    if check and not check.get("ok"):
        bad.append(f"Model unavailable: {check.get('reason') or 'cannot be called'}")
    if status == "error":
        bad.append(bot.get("error_message") or "Stopped on an error")
    elif status == "paused":
        warn.append(bot.get("error_message") or "Paused")

    last_at = parse_time((digest or {}).get("last_decision_at"))
    last_type = (digest or {}).get("last_action_type")
    if status == "running":
        beat = parse_time(bot.get("last_heartbeat"))
        if beat is None or (now - beat).total_seconds() > 90:
            bad.append("No heartbeat from the bot process in the last 90 seconds")
        bar_seconds = BOT_TIMEFRAME_MINUTES[normalize_bot_timeframe(bot.get("timeframe"))] * 60
        if normalize_bot_cadence(bot.get("decision_cadence")) == "candle_close":
            allowed_gap = 2 * bar_seconds + 300
        else:
            allowed_gap = max(3 * int(bot.get("cooldown_seconds") or 60), 180) + 120
        started = parse_time(bot.get("started_at"))
        reference = max([t for t in (last_at, started) if t is not None], default=None)
        # Rule bots log only when they act, so silence is not overdue.
        if not is_rule_bot(bot) and (reference is None or (now - reference).total_seconds() > allowed_gap):
            warn.append("No decision when one was due")
        # A decision from before this run started says nothing about this run.
        if started is not None and last_at is not None and last_at < started:
            last_type = None
        if last_type == "skipped":
            warn.append("Candles are stale, so the bot is skipping decisions")
        elif last_type == "error":
            warn.append("The last decision failed")

    if bad:
        level = "bad"
    elif warn:
        level = "warn"
    else:
        level = "ok" if status == "running" else "idle"
    return {
        "level": level,
        "reasons": bad + warn,
        "last_decision_at": last_at.isoformat() if last_at else None,
        "last_action_type": last_type,
    }


def api_fleet() -> dict:
    """One row of cockpit metrics per bot: sparkline, return against
    buy-and-hold, win rate, today's AI cost and a health dot."""
    from forven.bot_factory.performance import performance_start, sparkline
    from forven.db import get_bot_ai_usage, get_bot_trade_ledger, get_fleet_decision_digest

    bots = api_list_bots()
    digests = get_fleet_decision_digest()
    marks = _price_marks()
    now = _now_utc()
    ledgers = {b["id"]: get_bot_trade_ledger(b["id"]) for b in bots}
    starts = [
        performance_start(
            ledgers[b["id"]],
            first_decision_at=(digests.get(b["id"]) or {}).get("first_decision_at"),
            created_at=b.get("created_at"),
        )
        for b in bots
    ]
    earliest = min([s for s in starts if s is not None], default=None)
    prices = _benchmark_prices(earliest)

    rows = []
    for bot in bots:
        perf = _bot_performance(
            bot,
            ledger=ledgers[bot["id"]],
            digest=digests.get(bot["id"]),
            marks=marks,
            benchmark_prices=prices,
            ai_cost_usd=get_bot_ai_usage(bot["id"])["total_cost_usd"],
            funding_accrued=float(bot.get("funding_accrued") or 0),
        )
        summary = perf["summary"]
        rows.append({
            "bot_id": bot["id"],
            "sparkline": sparkline(perf["curve"]),
            "equity": summary["equity"],
            "return_pct": summary["return_pct"],
            "benchmark_return_pct": summary["benchmark_return_pct"],
            "excess_return_pct": summary["excess_return_pct"],
            "win_rate": summary["win_rate"],
            "closed_trades": summary["closed_trades"],
            "open_positions": summary["open_positions"],
            "ai_cost_today_usd": round(float(bot.get("llm_cost_today") or 0), 4),
            "ai_cost_usd": summary["ai_cost_usd"],
            "start_at": summary["start_at"],
            "health": _bot_health(bot, digests.get(bot["id"]), now),
        })
    return {"benchmark_symbol": BENCHMARK_SYMBOL, "bots": rows}


def api_get_decision(bot_id: str, decision_id: int) -> dict:
    """One decision with its prompt, reply and risk check (the inspector)."""
    from forven.db import get_bot_decision

    decision = get_bot_decision(bot_id, decision_id)
    if decision is None:
        raise LookupError(f"Decision {decision_id} not found for bot {bot_id}")
    return decision


RECOMMENDED_BOT_SETTINGS = {"decision_cadence": "candle_close"}


def api_apply_recommended(bot_id: str) -> dict:
    """Move an existing bot onto the recommended settings (decide on each new
    candle). A running bot picks the change up on its next tick."""
    if not get_bot(bot_id):
        raise LookupError(f"Bot {bot_id} not found")
    update_bot(bot_id, dict(RECOMMENDED_BOT_SETTINGS))
    return _with_model_check(get_bot(bot_id))


async def api_test_decision(config: dict, bot_id: str | None = None) -> dict:
    """Run one decision on live data with the editor's settings: the full
    prompt, the model's answer and the risk check, with nothing traded,
    logged or counted against the bot's daily caps. An existing bot's saved
    settings, open positions and P&L fill in what the editor leaves out."""
    import asyncio
    import os

    from forven.bot_factory.engine import preview_decision
    from forven.bot_factory.models import BotConfigCreate
    from forven.bot_factory.runner import BotRunner

    base = get_bot(bot_id) if bot_id else None
    if bot_id and not base:
        raise LookupError(f"Bot {bot_id} not found")
    merged = {**BotConfigCreate().model_dump(), **(base or {})}
    merged.update({k: v for k, v in (config or {}).items() if v is not None})

    positions: list[dict] = []
    realized_pnl = 0.0
    if base:
        positions = get_open_bot_positions(bot_id)
        realized_pnl = float((get_bot_equity_state(bot_id) or {}).get("realized_pnl") or 0)

    runner = BotRunner(bot_id or "test-decision", parent_pid=os.getpid())
    runner._config = merged
    snapshot = await asyncio.get_running_loop().run_in_executor(None, runner._fetch_market_snapshot)
    stale = (snapshot or {}).get("stale_pairs") or {}
    if not snapshot or not snapshot.get("pairs"):
        return {
            "ran": False,
            "reason": "No fresh candles for the bot's pairs, so a running bot would skip this cycle too.",
            "stale_pairs": stale,
        }
    result = await preview_decision(merged, snapshot, positions=positions, realized_pnl=realized_pnl)
    return {
        "ran": True,
        "pairs": sorted(snapshot["pairs"]),
        "stale_pairs": stale,
        "open_positions": len(positions),
        **result,
    }


def api_followable_strategies() -> list[dict]:
    """Paper and live strategies a Signal-follower bot can follow."""
    from forven.db import get_db

    with get_db() as conn:
        rows = conn.execute(
            """SELECT s.id, s.name, s.display_name, s.symbol, s.timeframe, s.status,
                      (SELECT COUNT(*) FROM trades t
                        WHERE t.strategy_id = s.id AND t.status = 'OPEN'
                          AND COALESCE(t.source, '') NOT LIKE 'bot:%') AS open_trades
                 FROM strategies s
                WHERE s.status IN ('paper', 'live_graduated')
             ORDER BY s.status DESC, s.id"""
        ).fetchall()
    return [
        {
            "id": r["id"],
            "name": r["display_name"] or r["name"] or r["id"],
            "symbol": r["symbol"],
            "timeframe": r["timeframe"],
            "stage": "live" if r["status"] == "live_graduated" else "paper",
            "open_trades": int(r["open_trades"] or 0),
        }
        for r in rows
    ]

