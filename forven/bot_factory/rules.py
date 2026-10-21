"""Rule bots: Grid, DCA and Signal follower. They decide in code, so they cost
nothing per decision, and they run in the same runner, sandbox and risk
checks as AI bots.

Each ``decide_*`` function is pure: it takes the bot's settings, the market
snapshot, its open lots and a small state dict the runner keeps between ticks,
and returns a ``RuleDecision``. Opens are sized here from the bot's maximum
position; the engine's risk check still runs on every open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

AI = "ai"
GRID = "grid"
DCA = "dca"
SIGNAL = "signal"
BOT_TYPES: tuple[str, ...] = (AI, GRID, DCA, SIGNAL)
RULE_TYPES: tuple[str, ...] = (GRID, DCA, SIGNAL)


def normalize_bot_type(value: object) -> str:
    text = str(value or AI).strip().lower()
    return text if text in BOT_TYPES else AI


def is_rule_bot(config: dict | None) -> bool:
    return normalize_bot_type((config or {}).get("bot_type")) in RULE_TYPES


def needs_ai(config: dict | None) -> bool:
    """Whether the bot calls a model: AI bots always, signal followers only
    when the AI approves each entry (hybrid mode)."""
    config = config or {}
    kind = normalize_bot_type(config.get("bot_type"))
    if kind == AI:
        return True
    return kind == SIGNAL and bool((config.get("rule_params") or {}).get("ai_approval"))


@dataclass
class RuleDecision:
    reasoning: str
    trades: list[dict] = field(default_factory=list)


def _num(value: object, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _price(market_event: dict | None, pair: str) -> float | None:
    data = ((market_event or {}).get("pairs") or {}).get(pair) or {}
    price = _num(data.get("current_price"))
    return price if price and price > 0 else None


def _equity(config: dict, realized_pnl: float) -> float:
    return float(_num(config.get("capital_allocation"), 0.0) or 0.0) + float(realized_pnl or 0.0)


def _max_lot_usd(config: dict, realized_pnl: float) -> float:
    return _equity(config, realized_pnl) * float(_num(config.get("max_position_pct"), 10.0) or 0.0) / 100.0


def _qty(usd: float, price: float) -> float:
    return round(usd / price, 6) if price > 0 and usd > 0 else 0.0


def _lots(positions: list[dict] | None, pair: str, direction: str = "long") -> list[dict]:
    return [
        p for p in (positions or [])
        if p.get("ticker") == pair and (p.get("direction") or "long") == direction
    ]


def coin_pair(asset: object) -> str:
    """'BTC', 'BTC-USDT' or 'BTC/USDT' → 'BTC/USDT'."""
    text = str(asset or "").strip().upper()
    if "/" in text:
        return text
    return f"{text.split('-', 1)[0]}/USDT" if text else ""


# ── Grid ──────────────────────────────────────────────────────────────


def grid_levels(params: dict) -> tuple[list[float], float]:
    """The buy levels and the step between them."""
    lower = _num(params.get("lower"))
    upper = _num(params.get("upper"))
    count = int(_num(params.get("levels"), 0) or 0)
    if not lower or not upper or upper <= lower or count < 2:
        return [], 0.0
    step = (upper - lower) / count
    return [lower + step * k for k in range(count)], step


def validate_grid(params: dict) -> str | None:
    if not params.get("pair"):
        return "Grid needs a pair."
    levels, _ = grid_levels(params)
    if not levels:
        return "Grid needs a lower price below the upper price and at least 2 levels."
    if int(_num(params.get("levels"), 0) or 0) > 100:
        return "Grid allows at most 100 levels."
    return None


def decide_grid(
    config: dict, params: dict, market_event: dict | None, positions: list[dict] | None,
    state: dict, realized_pnl: float = 0.0,
) -> RuleDecision:
    """Buy one lot each time the price falls through a grid level; each lot
    sells one step higher (its take-profit). Levels with an open lot wait,
    and at most one lot is bought per tick."""
    pair = coin_pair(params.get("pair"))
    price = _price(market_event, pair)
    levels, step = grid_levels(params)
    if not price or not levels:
        return RuleDecision("No price or grid for this tick.")
    previous = state.get("last_price")
    state["last_price"] = price
    if previous is None:
        return RuleDecision(f"Grid armed at ${price:,.4g}; buys start as the price falls through a level.")
    occupied = {
        round((float(p.get("entry_price") or 0) - levels[0]) / step)
        for p in _lots(positions, pair)
    }
    crossed = [
        k for k, level in enumerate(levels)
        if previous > level >= price and k not in occupied
    ]
    if not crossed:
        return RuleDecision(f"Price ${price:,.4g}; no grid level crossed.")
    # A gap through several levels buys the one nearest the price; the
    # others wait for the next fall through them.
    k = min(crossed)
    level = levels[k]
    lot_usd = _max_lot_usd(config, realized_pnl)
    qty = _qty(lot_usd, price)
    if qty <= 0:
        return RuleDecision("Grid lot size came out at zero.")
    return RuleDecision(
        f"Price fell through grid level {k + 1} (${level:,.4g}); buying one lot that sells at ${level + step:,.4g}.",
        [{
            "action": "BUY", "ticker": pair, "qty": qty,
            "take_profit_pct": round(((level + step) / price - 1) * 100, 4),
            "reasoning": f"Grid buy at level {k + 1}",
        }],
    )


# ── DCA ───────────────────────────────────────────────────────────────


def validate_dca(params: dict) -> str | None:
    if not params.get("pair"):
        return "DCA needs a pair."
    if params.get("take_profit_pct") is not None and not (_num(params.get("take_profit_pct")) or 0) > 0:
        return "DCA needs a take-profit above 0%."
    if int(_num(params.get("safety_orders"), 0) or 0) > 0 and not (_num(params.get("safety_step_pct")) or 0) > 0:
        return "DCA safety orders need a price step above 0%."
    return None


def decide_dca(
    config: dict, params: dict, market_event: dict | None, positions: list[dict] | None,
    state: dict, now: datetime, realized_pnl: float = 0.0,
) -> RuleDecision:
    """A deal opens with a base buy, adds safety buys each time the price
    falls another step below the last buy, and closes every lot when the price
    reaches the take-profit above the average entry. A new deal starts once the
    interval since the last one has passed."""
    pair = coin_pair(params.get("pair"))
    price = _price(market_event, pair)
    if not price:
        return RuleDecision("No price this tick.")
    lots = sorted(_lots(positions, pair), key=lambda p: str(p.get("opened_at") or ""))
    max_lot = _max_lot_usd(config, realized_pnl)
    base_usd = min(_num(params.get("order_usd"), max_lot) or max_lot, max_lot)
    take_profit = _num(params.get("take_profit_pct"), 2.0) or 2.0

    if lots:
        qty_total = sum(float(p.get("qty") or 0) for p in lots)
        cost = sum(float(p.get("qty") or 0) * float(p.get("entry_price") or 0) for p in lots)
        average = cost / qty_total if qty_total else 0.0
        if average and price >= average * (1 + take_profit / 100):
            return RuleDecision(
                f"Price ${price:,.4g} reached the take-profit {take_profit:g}% above the "
                f"average entry ${average:,.4g}; closing {len(lots)} lot(s).",
                [
                    {"action": "SELL", "ticker": pair, "qty": p.get("qty"), "trade_id": p.get("trade_id"),
                     "entry_price": p.get("entry_price"), "direction": "long", "reasoning": "DCA take-profit"}
                    for p in lots
                ],
            )
        safety_used = len(lots) - 1
        safety_max = int(_num(params.get("safety_orders"), 0) or 0)
        step = _num(params.get("safety_step_pct"), 0.0) or 0.0
        last_entry = float(lots[-1].get("entry_price") or 0)
        if safety_used < safety_max and step > 0 and last_entry and price <= last_entry * (1 - step / 100):
            scale = _num(params.get("safety_scale"), 1.0) or 1.0
            usd = min(base_usd * scale ** (safety_used + 1), max_lot)
            return RuleDecision(
                f"Price ${price:,.4g} is {step:g}% below the last buy; safety order "
                f"{safety_used + 1} of {safety_max} (${usd:,.2f}).",
                [{"action": "BUY", "ticker": pair, "qty": _qty(usd, price),
                  "reasoning": f"DCA safety order {safety_used + 1}"}],
            )
        return RuleDecision(
            f"Deal open: {len(lots)} lot(s), average ${average:,.4g}, target ${average * (1 + take_profit / 100):,.4g}."
        )

    interval = timedelta(hours=_num(params.get("interval_hours"), 24.0) or 24.0)
    last_start = state.get("last_deal_at")
    if last_start and now - last_start < interval:
        return RuleDecision(f"Next deal at {(last_start + interval).isoformat(timespec='minutes')}.")
    state["last_deal_at"] = now
    return RuleDecision(
        f"Starting a deal: base buy ${base_usd:,.2f} at ${price:,.4g}.",
        [{"action": "BUY", "ticker": pair, "qty": _qty(base_usd, price), "reasoning": "DCA base order"}],
    )


# ── Signal follower ───────────────────────────────────────────────────


def validate_signal(params: dict) -> str | None:
    if not params.get("strategy_id"):
        return "Signal follower needs a strategy."
    return None


def decide_signal(
    config: dict, params: dict, market_event: dict | None, positions: list[dict] | None,
    followed: list[dict], state: dict, realized_pnl: float = 0.0,
) -> RuleDecision:
    """Hold what the followed strategy holds: open when it opens a trade (after
    this bot started following), close when it closes one. ``followed`` is the
    strategy's currently open trades (asset, direction, opened_at)."""
    since = state.get("following_since")
    wanted = {
        (coin_pair(t.get("asset")), str(t.get("direction") or "long").lower())
        for t in followed
        if params.get("join_open_trades") or not since or str(t.get("opened_at") or "") >= since
    }
    held = {(p.get("ticker"), (p.get("direction") or "long")) for p in (positions or [])}
    trades: list[dict] = []
    notes: list[str] = []
    for pair, direction in sorted(held - wanted):
        lot = _lots(positions, pair, direction)[0]
        trades.append({
            "action": "SELL" if direction == "long" else "COVER", "ticker": pair,
            "qty": lot.get("qty"), "trade_id": lot.get("trade_id"), "entry_price": lot.get("entry_price"),
            "direction": direction, "reasoning": "The followed strategy closed this trade",
        })
        notes.append(f"close {direction} {pair}")
    lot_usd = _max_lot_usd(config, realized_pnl)
    for pair, direction in sorted(wanted - held):
        price = _price(market_event, pair)
        if not price:
            notes.append(f"no price for {pair}")
            continue
        trades.append({
            "action": "BUY" if direction == "long" else "SHORT", "ticker": pair,
            "qty": _qty(lot_usd, price), "reasoning": "The followed strategy opened this trade",
            "follow": True,
        })
        notes.append(f"open {direction} {pair}")
    if not trades:
        return RuleDecision("In step with the strategy." if held else "Waiting for the strategy's next trade.")
    return RuleDecision("Following the strategy: " + ", ".join(notes) + ".", trades)


def validate_rule_params(bot_type: str, params: dict | None) -> str | None:
    """A plain message when the settings can't run, else None."""
    kind = normalize_bot_type(bot_type)
    params = params or {}
    if kind == GRID:
        return validate_grid(params)
    if kind == DCA:
        return validate_dca(params)
    if kind == SIGNAL:
        return validate_signal(params)
    return None


def rule_pairs(config: dict, followed: list[dict] | None = None) -> list[str]:
    """The pairs a rule bot needs prices for."""
    params = config.get("rule_params") or {}
    kind = normalize_bot_type(config.get("bot_type"))
    if kind in (GRID, DCA):
        pair = coin_pair(params.get("pair"))
        return [pair] if pair else []
    if kind == SIGNAL:
        pairs = {coin_pair(t.get("asset")) for t in (followed or [])}
        if params.get("pair"):
            pairs.add(coin_pair(params["pair"]))
        return sorted(p for p in pairs if p)
    return []


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
