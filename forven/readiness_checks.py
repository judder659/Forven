"""Switch on the deflated-Sharpe gate and the research holdout once, at startup.

The live-capital readiness review (2026-10-01) found both overfitting guards off
by default: the gauntlet->paper gate never rejected on the Deflated Sharpe Ratio,
and research saw every bar the gauntlet then tested it on. The defaults are now
on, but a default alone reaches few installs: the pipeline config is stored
fully materialized (``policy.load_pipeline_config`` heals KV to the merged
dict), so an existing install keeps an explicit ``deflated_sharpe_gate_enabled:
False`` forever.

``apply_readiness_checks()`` runs from the API bootstrap. The first time, it
turns both guards on in the stored settings and records that it did, so an
operator who switches either off afterwards keeps it off. Every time, it stamps
the holdout's ``established_at`` when the holdout is on without one: strategies
created before that moment saw the held-back data and are exempt, and with no
stamp ``research_holdout.is_contaminated`` exempts every strategy, which would
leave the holdout sealing research while testing nothing.

Neither guard re-judges a strategy already in paper or live: the DSR gate runs
only on the gauntlet->paper hop, and the holdout exempts strategies created
before it was established.

A leaf like ``forven.research_holdout``: the caller passes the KV accessors, so
this module adds no edge into the import cluster around ``forven.api_core``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from forven.research_holdout import DEFAULTS as HOLDOUT_DEFAULTS
from forven.research_holdout import stamp_established

log = logging.getLogger(__name__)

READINESS_CHECKS_MIGRATION_KEY = "forven:migrations:readiness_checks_on_v1"
PIPELINE_STORAGE_KEY = "forven:pipeline_thresholds"
SETTINGS_STORAGE_KEY = "forven:settings"


def _enable_dsr_gate(pipeline_config: Any) -> bool:
    """Flip a stored ``deflated_sharpe_gate_enabled: False`` on (mutates). No stored
    key means the code default (on) already applies."""
    if not isinstance(pipeline_config, dict):
        return False
    rob = pipeline_config.get("robustness_thresholds")
    if not isinstance(rob, dict) or "deflated_sharpe_gate_enabled" not in rob:
        return False
    if rob["deflated_sharpe_gate_enabled"] is True:
        return False
    rob["deflated_sharpe_gate_enabled"] = True
    return True


def _update_holdout(settings: dict[str, Any], *, enable: bool) -> dict[str, Any]:
    """Enable (optionally) and stamp the stored research holdout block (mutates).

    Returns what changed: ``{"enabled": bool, "established_at": str | None}``.
    """
    changed: dict[str, Any] = {"enabled": False, "established_at": None}
    research = settings.get("research_settings")
    if not isinstance(research, dict):
        research = {}
    block = research.get("research_holdout")
    if not isinstance(block, dict):
        block = {}
    effective = bool(block.get("enabled", HOLDOUT_DEFAULTS["enabled"]))

    if enable and not effective:
        block["enabled"] = True
        changed["enabled"] = True
    elif effective and "enabled" not in block:
        # On by default with nothing stored: store it so the stamp below applies.
        block["enabled"] = True

    research["research_holdout"] = block
    before = str(block.get("established_at") or "").strip()
    stamp_established(research)
    after = str(block.get("established_at") or "").strip()
    if after and after != before:
        changed["established_at"] = after
    if changed["enabled"] or changed["established_at"]:
        settings["research_settings"] = research
    return changed


def apply_readiness_checks(
    kv_get: Callable[..., Any], kv_set: Callable[[str, Any], None]
) -> dict[str, Any]:
    """Turn the DSR gate and research holdout on once; keep the holdout stamped.

    Idempotent. Run it under the settings mutation lock. Returns a summary of
    what changed (for logs and tests).
    """
    first_run = not kv_get(READINESS_CHECKS_MIGRATION_KEY)
    summary: dict[str, Any] = {"first_run": first_run, "dsr_enabled": False, "holdout": {}}

    if first_run:
        pipeline_config = kv_get(PIPELINE_STORAGE_KEY)
        if _enable_dsr_gate(pipeline_config):
            kv_set(PIPELINE_STORAGE_KEY, pipeline_config)
            summary["dsr_enabled"] = True

    settings = kv_get(SETTINGS_STORAGE_KEY, {})
    if not isinstance(settings, dict):
        settings = {}
    summary["holdout"] = _update_holdout(settings, enable=first_run)
    if summary["holdout"]["enabled"] or summary["holdout"]["established_at"]:
        kv_set(SETTINGS_STORAGE_KEY, settings)

    if first_run:
        kv_set(READINESS_CHECKS_MIGRATION_KEY, {"applied_at": datetime.now(timezone.utc).isoformat()})

    if summary["dsr_enabled"] or summary["holdout"]["enabled"]:
        log.warning(
            "Readiness checks switched on: deflated-Sharpe gate=%s, research holdout=%s "
            "(established %s). Strategies already in paper or live are not re-judged; "
            "either can be turned off in Settings.",
            "on" if summary["dsr_enabled"] else "unchanged",
            "on" if summary["holdout"]["enabled"] else "unchanged",
            summary["holdout"]["established_at"] or "earlier",
        )
    elif summary["holdout"]["established_at"]:
        log.info("Research holdout established at %s", summary["holdout"]["established_at"])
    return summary
