"""Discord channel alias map — shared by the bot gateway and the API.

Lives outside ``forven/bot.py`` so API code can resolve aliases without
importing the discord package.

No channel ids ship with Forven: every install talks to its own Discord
server. An alias resolves, in order, to:

1. the id set for it in config.json ``discord_channels``;
2. a channel on the bot's server whose name matches the alias (or one of
   its fallback names, e.g. ``heartbeat`` -> ``#alerts``);
3. the ``general`` channel, else the first channel the bot can post to.

Steps 2 and 3 use the channel list the bot publishes when it connects, so
alerts reach a fresh install's server with no channel setup at all.
"""
from __future__ import annotations

from forven.config import load_config

# Alias -> server channel names it may land in, best match first. Old
# room-specific aliases collapse onto the reduced notification/channel model.
CHANNEL_ALIASES: dict[str, tuple[str, ...]] = {
    "general": ("general",),
    "ops": ("ops", "development"),
    "approvals": ("approvals", "alerts"),
    "risk": ("risk", "alerts"),
    "morning-brief": ("morning-brief", "briefs"),
    "evening-brief": ("evening-brief", "evening-summary", "briefs"),
    "evening-summary": ("evening-summary", "evening-brief", "briefs"),
    "chat": ("chat",),
    "heartbeat": ("heartbeat", "alerts"),
    "development": ("development", "ops"),
    "strategies": ("strategies",),
    "alerts": ("alerts",),
    "research": ("research", "quant-researcher"),
    "backtesting": ("backtesting", "backtest-engineer", "back-test-engineer"),
    "paper-trades": ("paper-trades", "trades"),
    "market-data": ("market-data",),
    "autopilot": ("autopilot",),
    "news": ("news", "sentiment"),
    "full-stack-engineer": ("full-stack-engineer", "development"),
    "quant-researcher": ("quant-researcher", "research"),
    "back-test-engineer": ("back-test-engineer", "backtest-engineer", "backtesting"),
    "risk-manager": ("risk-manager", "risk", "alerts"),
    "sentiment": ("sentiment", "sentiment-analyst", "news"),
    "full-stack-engineers": ("full-stack-engineers", "development", "ops"),
}

# Conversational reply channels must be a real match, never the catch-all.
_NO_FALLBACK = frozenset({"chat"})


def _configured_overrides() -> dict[str, str]:
    try:
        raw = load_config().get("discord_channels") or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v).strip() for k, v in raw.items() if str(v or "").strip()}


def load_channel_map(*, fallback: bool = True) -> dict[str, str]:
    """Alias -> channel-id map for this install.

    With ``fallback=False`` only configured or name-matched aliases appear,
    so callers can tell a real channel from the catch-all.
    """
    overrides = _configured_overrides()
    live = available_channels() or []
    by_name: dict[str, str] = {}
    for entry in live:
        by_name.setdefault(entry["name"].lower(), entry["id"])

    default_id = overrides.get("general") or by_name.get("general")
    if not default_id and live:
        default_id = live[0]["id"]

    out: dict[str, str] = {}
    for alias in {*CHANNEL_ALIASES, *overrides}:
        channel_id = overrides.get(alias)
        if not channel_id:
            for name in CHANNEL_ALIASES.get(alias, (alias,)):
                channel_id = by_name.get(name)
                if channel_id:
                    break
        if not channel_id and fallback and alias not in _NO_FALLBACK:
            channel_id = default_id
        if channel_id:
            out[alias] = channel_id
    return out


def resolve_channel_id(alias: str | None) -> str | None:
    """Channel id for an alias (or a raw numeric id), freshly resolved."""
    name = str(alias or "").strip()
    if not name:
        return None
    if name.isdigit():
        return name
    return load_channel_map().get(name)


# The bot gateway publishes the text channels it can actually post to (per
# guild it is connected to) under this kv key on connect and on channel
# create/delete/rename. This is what makes alias-by-name resolution work
# on ANY user's Discord server — the alias map above is only a fallback for
# setups where the bot has never connected.
AVAILABLE_CHANNELS_KV_KEY = "discord:available_channels"


def available_channels() -> list[dict[str, str]] | None:
    """Live guild text-channel list published by the bot, or None if the bot
    has never published (callers fall back to the static alias map)."""
    try:
        from forven.db import kv_get

        data = kv_get(AVAILABLE_CHANNELS_KV_KEY)
        raw = data.get("channels") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return None
        out: list[dict[str, str]] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            channel_id = str(entry.get("id") or "").strip()
            name = str(entry.get("name") or "").strip()
            if channel_id and name:
                out.append({"id": channel_id, "name": name})
        return out or None
    except Exception:
        return None
