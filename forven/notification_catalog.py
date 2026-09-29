"""The notification catalog: every kind of event Forven tells the operator about.

One category per kind of event. Each category names the channels it can reach
(an in-app pop-up, Discord) and the preference keys that switch them. Sidebar
badges are listed separately because they describe a page, not an event.

Owned here and consumed by:
  * ``forven.notification_policy``: builds the preference defaults and decides
    which Discord switch governs an event (``category_for_event``);
  * the live WebSocket: tags every new notification with its category, so the
    frontend can decide on a pop-up without a mapping of its own;
  * ``frontend/src/lib/notifications/catalog.generated.json``: a checked-in
    snapshot read by the Settings page and the pop-up router. Regenerate it with
    ``python -m forven.notification_catalog``; tests/test_notification_catalog.py
    keeps it fresh.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

GENERATED_CATALOG_PATH = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "src"
    / "lib"
    / "notifications"
    / "catalog.generated.json"
)

NOTIFICATION_GROUPS: tuple[tuple[str, str], ...] = (
    ("trading", "Trading"),
    ("approvals_risk", "Approvals & risk"),
    ("strategies_jobs", "Strategies & jobs"),
    ("system", "System & agents"),
)


@dataclass(frozen=True)
class NotificationCategory:
    id: str
    group: str
    label: str
    description: str
    # Default for the pop-up switch; None means this event never pops up.
    popup: bool | None
    # Safety alerts: the pop-up cannot be switched off.
    popup_locked: bool = False
    # notification_preferences keys that switch Discord delivery (all set together).
    discord_keys: tuple[str, ...] = ()
    # Always delivered to Discord while Discord is connected and not muted.
    discord_locked: bool = False
    discord_note: str = ""

    @property
    def popup_key(self) -> str | None:
        if self.popup is None or self.popup_locked:
            return None
        return f"popup_{self.id}"


@dataclass(frozen=True)
class NavBadge:
    id: str
    href: str
    label: str
    description: str
    default: bool
    # 'total': the number of items open right now, shown until they are resolved.
    # 'unread': only items that arrived since the page was last opened.
    mode: str

    @property
    def key(self) -> str:
        return f"badge_{self.id}"


NOTIFICATION_CATEGORIES: tuple[NotificationCategory, ...] = (
    # -- Trading -------------------------------------------------------------
    NotificationCategory(
        "live_trade_opened",
        "trading",
        "Live trade opened",
        "A strategy opened a real-money position.",
        popup=True,
        discord_keys=("trade_opened_to_discord",),
    ),
    NotificationCategory(
        "live_trade_closed",
        "trading",
        "Live trade closed",
        "A real-money position closed, with its P&L.",
        popup=True,
        discord_keys=("trade_closed_to_discord",),
    ),
    NotificationCategory(
        "live_order_failure",
        "trading",
        "Live order failed",
        "A live order failed, or a filled position was left without its stop.",
        popup=True,
        popup_locked=True,
        discord_keys=("trade_failed_to_discord",),
    ),
    NotificationCategory(
        "live_entry_blocked",
        "trading",
        "Live entry blocked",
        "A risk gate refused a live entry. At most one alert per strategy and cause each hour.",
        popup=True,
        discord_keys=("trade_blocked_to_discord",),
    ),
    NotificationCategory(
        "paper_trade_opened",
        "trading",
        "Paper trade opened",
        "A paper strategy opened a simulated position.",
        popup=False,
        discord_keys=("paper_trade_opened_to_discord",),
    ),
    NotificationCategory(
        "paper_trade_closed",
        "trading",
        "Paper trade closed",
        "A simulated position closed, with its P&L.",
        popup=False,
        discord_keys=("paper_trade_closed_to_discord",),
    ),
    NotificationCategory(
        "bot_trades",
        "trading",
        "Bot Factory trades",
        "A Bot Factory bot opened or closed a position.",
        popup=True,
    ),
    # -- Approvals & risk ----------------------------------------------------
    NotificationCategory(
        "approval_required",
        "approvals_risk",
        "Approval needed",
        "A promotion or agent action is waiting for your decision.",
        popup=True,
        discord_keys=("approval_required_to_discord",),
    ),
    NotificationCategory(
        "approval_resolved",
        "approvals_risk",
        "Approval resolved",
        "A pending approval was approved, denied or expired.",
        popup=None,
        discord_keys=("approval_resolved_to_discord",),
    ),
    NotificationCategory(
        "risk_halt",
        "approvals_risk",
        "Kill switch & loss halts",
        "Trading was halted or resumed by the kill switch or the daily loss limit.",
        popup=True,
        popup_locked=True,
    ),
    NotificationCategory(
        "risk_alerts",
        "approvals_risk",
        "Risk alerts",
        "Liquidation distance, drawdown, the decay kill switch and equity anomalies.",
        popup=True,
        discord_keys=("risk_critical_to_discord",),
    ),
    # -- Strategies & jobs ---------------------------------------------------
    NotificationCategory(
        "pipeline_transition",
        "strategies_jobs",
        "Strategy stage changes",
        "A strategy moved between pipeline stages.",
        popup=False,
        discord_keys=("pipeline_transition_to_discord",),
    ),
    NotificationCategory(
        "background_jobs",
        "strategies_jobs",
        "Background jobs",
        "A job or scan you started finished or failed.",
        popup=True,
    ),
    # -- System & agents -----------------------------------------------------
    NotificationCategory(
        "system_critical",
        "system",
        "Critical system alerts",
        "A core service is down, a circuit breaker tripped, or data integrity failed.",
        popup=True,
        discord_locked=True,
        discord_note="Always sent while Discord is connected.",
    ),
    NotificationCategory(
        "system_warning",
        "system",
        "System warnings",
        "Degraded services, execution-quality drift and other warnings. Paper-only warnings stay in the app.",
        popup=False,
        discord_keys=("system_degraded_to_discord",),
    ),
    NotificationCategory(
        "system_recovered",
        "system",
        "System recovered",
        "A service that was degraded is healthy again.",
        popup=False,
        discord_keys=("system_recovered_to_discord",),
    ),
    NotificationCategory(
        "agent_failure",
        "system",
        "Agent failures",
        "An agent task failed or an agent raised an alert.",
        popup=False,
        discord_keys=("agent_failure_to_discord",),
    ),
    NotificationCategory(
        "agent_completion",
        "system",
        "Agent task finished",
        "An agent completed a task.",
        popup=None,
        discord_keys=("agent_completion_to_discord",),
    ),
    NotificationCategory(
        "bug_report",
        "system",
        "Agent bug reports",
        "An agent filed a bug report about the system.",
        popup=None,
        discord_keys=("bug_report_to_discord",),
    ),
    NotificationCategory(
        "mcp_session",
        "system",
        "AI client connected",
        "An AI client such as Claude Code or Codex connected to Forven over MCP.",
        popup=False,
    ),
    NotificationCategory(
        "digests",
        "system",
        "Daily summaries",
        "The morning brief and evening summary.",
        popup=None,
        discord_keys=("digests_to_discord",),
    ),
)

NAV_BADGES: tuple[NavBadge, ...] = (
    NavBadge(
        "approvals",
        "/approval",
        "Approvals",
        "Approvals waiting for your decision.",
        default=True,
        mode="total",
    ),
    NavBadge(
        "diagnostics",
        "/diagnostics",
        "Diagnostics",
        "System issues raised since you last opened Diagnostics.",
        default=True,
        mode="unread",
    ),
    NavBadge(
        "data",
        "/data",
        "Data",
        "Market data behind a live or paper strategy that is running late.",
        default=True,
        mode="total",
    ),
    NavBadge(
        "live_trades",
        "/live-trades",
        "Live Trades",
        "Open live positions.",
        default=True,
        mode="total",
    ),
    NavBadge(
        "paper_trades",
        "/paper-trades",
        "Paper Trades",
        "Open paper positions.",
        default=False,
        mode="total",
    ),
    NavBadge(
        "bot_factory",
        "/bot-factory",
        "Bot Factory",
        "Bots that are running.",
        default=True,
        mode="total",
    ),
)

# The test event behind "Send a test notification": always pops up and always
# goes to Discord when connected, whatever the switches say.
TEST_CATEGORY = "test"

_CATEGORY_BY_ID = {category.id: category for category in NOTIFICATION_CATEGORIES}

_SEVERITY_ALIASES = {
    "warning": "warn",
    "error": "fail",
    "err": "fail",
    "failed": "fail",
    "failure": "fail",
    "crit": "critical",
    "fatal": "critical",
}

_EVENT_CATEGORIES: dict[str, str] = {
    "trade_failed": "live_order_failure",
    "trade_protective_unarmed": "live_order_failure",
    "trade_fill_persistence_failed": "live_order_failure",
    "mainnet_unarmed_exit": "live_order_failure",
    "propr_mirror_unprotected": "live_order_failure",
    "trade_blocked": "live_entry_blocked",
    "trading_long_only": "live_entry_blocked",
    "approval_required": "approval_required",
    "approval_resolved": "approval_resolved",
    "risk_critical": "risk_alerts",
    "equity_anomaly": "risk_alerts",
    "risk_alert": "risk_alerts",
    "pipeline_transition": "pipeline_transition",
    "pipeline_hygiene": "pipeline_transition",
    "system_recovered": "system_recovered",
    "health_recovery": "system_recovered",
    "agent_task_failed": "agent_failure",
    "agent_alert": "agent_failure",
    "agent_task_completed": "agent_completion",
    "bug_report": "bug_report",
    "digest_ops": "digests",
    "digest_trading": "digests",
    "digest_daily": "digests",
    "digest_weekly": "digests",
    "overnight_summary": "digests",
    "notification_test": TEST_CATEGORY,
}

_TRADE_EVENT_CATEGORIES: dict[str, tuple[str, str]] = {
    "trade_opened": ("live_trade_opened", "paper_trade_opened"),
    "trade_closed": ("live_trade_closed", "paper_trade_closed"),
}


def normalize_severity(value: object) -> str:
    """Canonical severity: info | warn | fail | critical.

    Emitters have written 'warning' and 'error' over the years; every filter,
    rank and routing rule speaks the short forms, so aliases are folded here.
    """
    severity = str(value or "info").strip().lower() or "info"
    return _SEVERITY_ALIASES.get(severity, severity)


def execution_scope(metadata: Mapping[str, Any] | None) -> str | None:
    """'live', 'paper' or None, read from the metadata keys emitters use."""
    if not isinstance(metadata, Mapping):
        return None
    for key in ("execution_mode", "execution_type", "bucket"):
        raw = str(metadata.get(key) or "").strip().lower()
        if not raw:
            continue
        if raw in {"live", "mainnet"} or raw.startswith("live"):
            return "live"
        if raw in {"paper", "replay", "sim", "simulation"} or raw.startswith("paper"):
            return "paper"
    return None


def category_for_event(
    event_type: object,
    severity: object = "info",
    metadata: Mapping[str, Any] | None = None,
) -> str | None:
    """The catalog category an event belongs to, or None for plain info."""
    normalized = str(event_type or "").strip().lower()
    if normalized in _TRADE_EVENT_CATEGORIES:
        live, paper = _TRADE_EVENT_CATEGORIES[normalized]
        return live if execution_scope(metadata) == "live" else paper
    if normalized in _EVENT_CATEGORIES:
        return _EVENT_CATEGORIES[normalized]
    # Everything else is a system event, sorted by how loud it is.
    level = normalize_severity(severity)
    if level == "critical":
        return "system_critical"
    if level in {"warn", "fail"}:
        return "system_warning"
    return None


def get_category(category_id: str | None) -> NotificationCategory | None:
    return _CATEGORY_BY_ID.get(str(category_id or ""))


def popup_preference_defaults() -> dict[str, bool]:
    return {
        category.popup_key: bool(category.popup)
        for category in NOTIFICATION_CATEGORIES
        if category.popup_key
    }


def badge_preference_defaults() -> dict[str, bool]:
    return {badge.key: badge.default for badge in NAV_BADGES}


def catalog_payload(preference_defaults: Mapping[str, Any]) -> dict[str, Any]:
    """The catalog as the frontend reads it, with defaults resolved."""

    def _discord(category: NotificationCategory) -> dict[str, Any] | None:
        if category.discord_locked:
            return {"keys": [], "default": True, "locked": True, "note": category.discord_note}
        if not category.discord_keys:
            return None
        return {
            "keys": list(category.discord_keys),
            "default": all(bool(preference_defaults.get(key, True)) for key in category.discord_keys),
            "locked": False,
            "note": category.discord_note,
        }

    def _popup(category: NotificationCategory) -> dict[str, Any] | None:
        if category.popup is None:
            return None
        return {
            "key": category.popup_key,
            "default": bool(category.popup),
            "locked": category.popup_locked,
        }

    return {
        "groups": [{"id": group_id, "label": label} for group_id, label in NOTIFICATION_GROUPS],
        "categories": [
            {
                "id": category.id,
                "group": category.group,
                "label": category.label,
                "description": category.description,
                "popup": _popup(category),
                "discord": _discord(category),
            }
            for category in NOTIFICATION_CATEGORIES
        ],
        "badges": [
            {
                "id": badge.id,
                "href": badge.href,
                "label": badge.label,
                "description": badge.description,
                "key": badge.key,
                "default": badge.default,
                "mode": badge.mode,
            }
            for badge in NAV_BADGES
        ],
    }


def generated_catalog_json() -> str:
    from forven.notification_policy import default_notification_preferences

    payload = {
        "__generated__": "python -m forven.notification_catalog — do not edit by hand",
        **catalog_payload(default_notification_preferences()),
    }
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def write_generated_catalog(path: Path = GENERATED_CATALOG_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(generated_catalog_json(), encoding="utf-8", newline="\n")
    return path


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    print(write_generated_catalog())
