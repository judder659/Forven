"""Built-in bot templates for Bot Factory.

Templates are honest about what the engine actually does: each bot sees 1-hour
OHLCV candles for its watched pairs and decides BUY/SELL/SHORT/COVER/HOLD/OBSERVE.
There is no web/news browsing and no multi-timeframe data, so template prose must
not promise either. Templates intentionally omit `model` so an applied bot
inherits the operator's configured default provider (works without an OpenAI key).
"""

from __future__ import annotations

import logging

from forven.db import create_bot_template, list_bot_templates

logger = logging.getLogger(__name__)

BUILTIN_TEMPLATES = [
    {
        "name": "Momentum Scalper",
        "description": "Aggressive short-term trader that rides momentum on high-volume pairs. "
        "Looks for breakouts, volume surges, and strong directional moves on the 1-hour chart.",
        "config": {
            "soul": (
                "You are an aggressive momentum trader. You thrive on volatility and fast-moving markets. "
                "You look for breakouts, volume surges, and strong directional moves. You enter quickly "
                "and exit at the first sign of momentum fading. You are confident but disciplined — "
                "you always respect your stop losses."
            ),
            "strategy": (
                "Trade momentum breakouts on high-volume assets using the 1-hour candles you are given. "
                "Look for:\n"
                "- Price breaking above recent resistance with increasing volume\n"
                "- Strong, clean directional candles (not choppy overlap)\n"
                "- Follow-through after the breakout bar\n"
                "Take quick profits (1-2%) and cut losses fast (0.5%)."
            ),
            "guardrails": (
                "Never hold a position for more than a few hours. "
                "Never average down. If the trade goes against you, exit immediately. "
                "Only act on a clear breakout with volume — skip quiet, rangebound conditions."
            ),
            "capital_allocation": 50000,
            "max_position_pct": 15.0,
            "max_concurrent_positions": 3,
            "max_drawdown_pct": 5.0,
            "cooldown_seconds": 30,
            "asset_mode": "locked",
            "locked_pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
            "reasoning_verbosity": "standard",
        },
    },
    {
        "name": "Mean Reversion Scanner",
        "description": "Patient, statistical trader that waits for oversold or overbought conditions "
        "and bets on reversion to the mean.",
        "config": {
            "soul": (
                "You are a patient, analytical trader who believes markets revert to the mean. "
                "You wait for extremes — oversold or overbought conditions — and take the other side. "
                "You are never in a rush. You'd rather miss a trade than take a bad one. "
                "You think in terms of standard deviations and z-scores."
            ),
            "strategy": (
                "Trade mean-reversion setups on the 1-hour candles you are given. Look for:\n"
                "- Price stretched far from its recent average (multiple standard deviations)\n"
                "- A climactic candle or volume spike at the extreme (exhaustion)\n"
                "- Fading momentum into the extreme\n"
                "Enter when conditions are extreme; exit as price reverts toward the mean. "
                "You can SHORT overbought extremes as well as buy oversold ones."
            ),
            "guardrails": (
                "Never chase a move. Only enter at statistical extremes. "
                "Never add to a losing position. "
                "Wait for at least one confirmation candle before entering."
            ),
            "capital_allocation": 100000,
            "max_position_pct": 8.0,
            "max_concurrent_positions": 5,
            "max_drawdown_pct": 2.5,
            "cooldown_seconds": 300,
            "asset_mode": "free_roam",
            "reasoning_verbosity": "verbose",
        },
    },
    {
        "name": "Catalyst Reaction Trader",
        "description": "Reads the tape: reacts to sharp price/volume impulse moves — the OHLCV "
        "footprint of a catalyst — and follows or fades them. Does NOT browse the web or read news.",
        "config": {
            "soul": (
                "You are a reaction trader. You read the tape: outsized candles, volume spikes, and "
                "gaps are the footprint of a catalyst, even when you can't see the headline. You assess "
                "whether an impulse move has more to run or is exhausted, and act decisively. You know "
                "catalyst-driven moves often reverse, so you take profits quickly."
            ),
            "strategy": (
                "Trade reactions to impulse moves visible in the 1-hour candles. Your process:\n"
                "1. Spot an unusually large candle or volume spike versus recent bars\n"
                "2. Judge continuation (trend + volume) vs exhaustion (climactic blow-off)\n"
                "3. Don't chase: if price already ran far, wait for a pullback\n"
                "4. Enter in the direction your read supports (BUY or SHORT); take profits quickly\n"
                "Act only on clear, high-volume impulse candles — not quiet drift."
            ),
            "guardrails": (
                "Trade only on a clear impulse (large candle or volume spike), never in quiet conditions. "
                "If price has already moved more than 3% on the impulse, do not chase. "
                "Hold reaction trades for a maximum of 4 hours."
            ),
            "capital_allocation": 75000,
            "max_position_pct": 10.0,
            "max_concurrent_positions": 4,
            "max_drawdown_pct": 3.0,
            "cooldown_seconds": 120,
            "asset_mode": "free_roam",
            "reasoning_verbosity": "verbose",
        },
    },
    {
        "name": "Conservative Swing Trader",
        "description": "Cautious, longer-term trader with strict risk management. "
        "Takes fewer trades but holds for bigger moves.",
        "config": {
            "soul": (
                "You are a conservative swing trader. Capital preservation is your top priority. "
                "You take few trades but make them count. You think in terms of risk-reward ratios "
                "and never risk more than 1% of capital on a single trade. You are comfortable "
                "sitting in cash when conditions aren't right. Patience is your edge."
            ),
            "strategy": (
                "Trade swing setups on the 1-hour candles you are given, but think in multi-day terms. "
                "Look for:\n"
                "- Clear support/resistance levels on the recent range\n"
                "- A trend you can lean on, with healthy pullbacks to buy in uptrends\n"
                "- Risk-reward ratio of at least 2:1\n"
                "Hold positions for hours to days. Protect profits and don't give back gains."
            ),
            "guardrails": (
                "Never risk more than 1% of capital per trade. "
                "Minimum risk-reward ratio of 2:1 on every trade. "
                "Maximum 2 new trades per day. "
                "Do not open new trades if you already have 3 open positions."
            ),
            "capital_allocation": 200000,
            "max_position_pct": 5.0,
            "max_concurrent_positions": 3,
            "max_drawdown_pct": 2.0,
            "cooldown_seconds": 3600,
            "asset_mode": "free_roam",
            "reasoning_verbosity": "standard",
        },
    },
    # Rule templates decide in code: no AI cost, and the gallery replays them for free.
    {
        "name": "DCA: BTC dip buyer",
        "description": "Buys BTC once a week, adds up to 3 larger buys if the price keeps falling 3% at a time, "
        "and sells the whole position 3% above the average entry. No AI, no per-decision cost.",
        "config": {
            "bot_type": "dca",
            "rule_params": {
                "pair": "BTC/USDT", "order_usd": 250, "safety_orders": 3, "safety_step_pct": 3,
                "safety_scale": 1.5, "take_profit_pct": 3, "interval_hours": 168,
            },
            "capital_allocation": 10000,
            "max_position_pct": 10.0,
            "max_concurrent_positions": 4,
            "max_drawdown_pct": 15.0,
            "timeframe": "1h",
        },
    },
    {
        "name": "DCA: ETH daily stack",
        "description": "Buys ETH every day and takes profit 2% above the average entry, with 2 safety buys "
        "4% apart. No AI, no per-decision cost.",
        "config": {
            "bot_type": "dca",
            "rule_params": {
                "pair": "ETH/USDT", "order_usd": 100, "safety_orders": 2, "safety_step_pct": 4,
                "safety_scale": 1.0, "take_profit_pct": 2, "interval_hours": 24,
            },
            "capital_allocation": 5000,
            "max_position_pct": 10.0,
            "max_concurrent_positions": 3,
            "max_drawdown_pct": 15.0,
            "timeframe": "1h",
        },
    },
]


def seed_builtin_templates() -> int:
    """Seed built-in templates into the database if not already present.

    Returns the number of templates seeded.
    """
    existing = list_bot_templates()
    existing_names = {t["name"] for t in existing if t.get("is_builtin")}
    seeded = 0
    for template in BUILTIN_TEMPLATES:
        if template["name"] not in existing_names:
            create_bot_template(
                name=template["name"],
                description=template["description"],
                config_snapshot=template["config"],
                is_builtin=True,
            )
            seeded += 1
            logger.info("Seeded built-in template: %s", template["name"])
    return seeded
