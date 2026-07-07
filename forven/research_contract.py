from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_DEFAULT_RESEARCH_SETTINGS: dict[str, Any] = {
    "external_benchmarking_enabled": True,
    # Autonomous external-source harvesting (the scheduled crucible-discovery job).
    # OFF by default = operator-approves: the operator triggers/reviews discovery.
    # Flip enabled=True (and optionally mode="autonomous") to let it run on schedule.
    "autonomous_discovery": {
        "enabled": False,
        "mode": "operator_approves",
        "max_open_discovery_tasks": 1,
    },
    "lane_weights": {
        "exploration": 0.3,
        "exploitation": 0.5,
        "benchmarking": 0.2,
    },
    "spawn_limits": {
        "per_run": 3,
        "rolling_window": 8,
        "window_days": 7,
    },
    "memory_modes": {
        "exploration": {
            "constraint_memory": True,
            "inspiration_memory": "optional",
        },
        "exploitation": {
            "constraint_memory": True,
            "inspiration_memory": "bounded",
        },
        "benchmarking": {
            "constraint_memory": True,
            "inspiration_memory": "bounded",
        },
    },
    "allowed_external_source_types": [
        "reddit",
        "youtube",
        "podcast",
        "blog",
        "github",
        "forum",
        "book",
        "paper",
    ],
    "research_sources": {
        # Enabled by default so autonomous benchmarking-lane agents discover
        # from all source types (parity with youtube, which has no per-source
        # gate). Operators can disable individual sources via the Research
        # Settings UI if a given source becomes noisy or rate-limited.
        "reddit": {
            "enabled": True,
            "subs": ["algotrading", "quant", "options", "thetagang", "systematictrading"],
            "client_id": None,
            "client_secret": None,
            "rate_limit_per_min": 30,
        },
        "blog": {
            "enabled": True,
            "feeds": [
                "https://www.quantstart.com/articles/rss/",
                "https://quantocracy.com/feed/",
                "https://blog.quantinsti.com/feed/",
            ],
            "rate_limit_per_min": 30,
        },
        "podcast": {
            # Trading/quant podcast RSS feeds. Show-notes are harvested by default;
            # audio transcription is a pluggable hook (OFF until a backend is set).
            "enabled": True,
            "feeds": [
                "https://chatwithtraders.com/feed/podcast/",
                "https://feeds.megaphone.fm/topdogtrading",
            ],
            "rate_limit_per_min": 20,
        },
        "github": {
            "enabled": True,
            "orgs": ["quantopian", "hudson-and-thames", "stefan-jansen"],
            "personal_access_token": None,
            "rate_limit_per_min": 60,
        },
        "forum": {
            "enabled": True,
            "sites": ["elitetrader.com", "quantconnect.com", "quantnet.com"],
            "rate_limit_per_min": 20,
        },
    },
    "hypothesis_discipline": {
        "active_pool_cap": 100,
        "min_strategies_per_pick": 3,
        "revisit_interval_days": 90,
        "verdict_hit_rate_threshold": 0.4,
        "verdict_min_diversity_cells": 4,
        "verdict_rolling_window": 10,
        # Oversaturation remediation (2026-06-05). The active-pool cap alone only
        # bounds the TOTAL pool; without these the pool fills with un-started
        # 'proposed' crucibles that never generate strategies. See
        # docs/reviews / memory project_crucible_oversaturation_2026_06_05.
        # Soft cap on un-started (proposed, 0 live-strategy) crucibles: once the
        # un-refined backlog reaches this, research agents are steered to refine /
        # expand existing crucibles instead of minting fresh ones.
        "max_unrefined_active": 30,
        # Drain: a 'proposed', 0-live-strategy crucible idle (never dispatched and
        # untouched) this many days is archived (archive_reason='unstarted_ageout')
        # so the pool reflects real research instead of an idle backlog.
        "unstarted_ageout_days": 7,
        # Reserved strategy-developer in-flight slots for refine_crucible (the
        # proposed->researching funnel feeder), carved out so the promotion loop's
        # develop_candidate work can't monopolize every slot and starve the funnel.
        "refine_in_flight_budget": 2,
        # CRUX-1 (2026-07-06): hard daily cap on develop_candidate dispatches
        # shared by the crucible planner AND the hypothesis-promotion loop.
        # In-flight caps bound concurrency, not spend — pre-CRUX the loops
        # burned ~190 develops/day for ~1 survivor per ~250 develops. Value
        # ranking decides WHO gets the budget; this bounds HOW MUCH exists.
        "crucible_daily_develop_budget": 150,
        # CRUX-1: percent of daily develops stamped with an explicit
        # short/both trade_mode requirement (graveyard audit: shorts
        # net-positive in every regime at 9:1 under-generation). 0 disables.
        "crucible_short_mode_quota_pct": 30,
        # CRUX-1: percent of daily develops stamped with an orthogonal-data
        # requirement (primary signal from funding/basis/OI/positioning/IV
        # instead of price-only indicators — the graveyard's most-mined
        # field). Independent of the short quota. 0 disables.
        "crucible_orthogonal_data_quota_pct": 40,
        # SURV-QUOTA-1 (2026-07-07): percent of daily develops reserved for
        # NEIGHBORHOOD VARIANTS of this instance's OWN proven survivors
        # (paper/live strategies) — same family skeleton, varied along the
        # axes known to transfer (asset, timeframe, confirmation gate). The
        # quota is instance-relative by construction: a fresh install spends
        # nothing here until it has a survivor, and nothing about WHICH family
        # to exploit ever ships in the product. 0 disables.
        "crucible_survivor_neighborhood_quota_pct": 25,
        # Per-family ceiling WITHIN the survivor quota (percent of the day's
        # survivor-directed develops one family may consume) — the exploit
        # lane must not monoculture a single lucky family.
        "survivor_neighborhood_family_cap_pct": 50,
        # Autonomous-mint dedup (2026-06-10 audit B-16). An agent create_hypothesis
        # is rejected when its title duplicates an active crucible or one disproven
        # within this many days — stops the re-mint/re-disprove churn loop. 0
        # disables the disproven-cooldown arm (active-pool dedup is always on).
        "disproven_dedup_lookback_days": 30,
        # Graveyard-aware novelty (autonomous-only). The title dedup above is literal;
        # it misses semantically-equivalent re-treads (e.g. the 30+ disproven SOL+EMA
        # crucibles with differing titles). These discount the LLM's self-reported
        # novelty by how many times the proposed idea-cluster (family x asset) has
        # already been DISPROVEN, so a settled idea-space stops being scored novel and
        # loses the dispatch queue. SOFT by default (downgrade); hard-block disabled.
        #   scale: disproven count at which the novelty factor reaches 0.5.
        #   min_total: don't penalize until the instance has this many hypotheses
        #     (so small/new instances are never throttled).
        #   hard_block: refuse the autonomous mint once the cluster has >= this many
        #     disproven members; 0 disables the hard arm (soft downgrade only).
        "novelty_graveyard_scale": 4.0,
        "novelty_graveyard_min_total": 40,
        "novelty_graveyard_hard_block": 0,
    },
}


_HYPOTHESIS_DISCIPLINE_RANGES: dict[str, tuple[int | float, int | float]] = {
    "active_pool_cap": (1, 500),
    "min_strategies_per_pick": (1, 20),
    "revisit_interval_days": (7, 365),
    "verdict_hit_rate_threshold": (0.0, 1.0),
    "verdict_min_diversity_cells": (1, 50),
    "verdict_rolling_window": (3, 100),
    "max_unrefined_active": (1, 500),
    "unstarted_ageout_days": (1, 365),
    "refine_in_flight_budget": (0, 10),
    "crucible_daily_develop_budget": (1, 2000),
    "crucible_short_mode_quota_pct": (0, 100),
    "crucible_orthogonal_data_quota_pct": (0, 100),
    "crucible_survivor_neighborhood_quota_pct": (0, 100),
    "survivor_neighborhood_family_cap_pct": (0, 100),
    "disproven_dedup_lookback_days": (0, 365),
    "novelty_graveyard_scale": (1.0, 100.0),
    "novelty_graveyard_min_total": (0, 5000),
    "novelty_graveyard_hard_block": (0, 500),
}


def get_hypothesis_discipline_settings(raw_settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the hypothesis_discipline sub-block from effective settings, clamped to safe ranges.

    Out-of-range overrides are silently clamped to the nearest valid bound; missing keys
    fall back to defaults. Callers can rely on every key being present and in-range.
    """
    effective = get_effective_research_settings(raw_settings)
    block = effective.get("hypothesis_discipline")
    defaults = _DEFAULT_RESEARCH_SETTINGS["hypothesis_discipline"]
    if not isinstance(block, Mapping):
        return dict(defaults)
    out: dict[str, Any] = {}
    for key, default_value in defaults.items():
        value = block.get(key, default_value)
        lo, hi = _HYPOTHESIS_DISCIPLINE_RANGES[key]
        if isinstance(default_value, float):
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = float(default_value)
            value = max(float(lo), min(float(hi), value))
        else:
            try:
                value = int(value)
            except (TypeError, ValueError):
                value = int(default_value)
            value = max(int(lo), min(int(hi), value))
        out[key] = value
    return out

_LANE_ORDER = ("exploration", "exploitation", "benchmarking")


@dataclass(frozen=True, slots=True)
class ResearchContract:
    lane: str
    available_datasets: list[str]
    memory_mode: dict[str, Any]
    external_sources_allowed: bool
    allowed_external_source_types: list[str]
    novelty_threshold: float
    spawn_limits: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "available_datasets": list(self.available_datasets),
            "memory_mode": dict(self.memory_mode),
            "external_sources_allowed": self.external_sources_allowed,
            "allowed_external_source_types": list(self.allowed_external_source_types),
            "novelty_threshold": self.novelty_threshold,
            "spawn_limits": dict(self.spawn_limits),
        }


def default_research_settings() -> dict[str, Any]:
    return deepcopy(_DEFAULT_RESEARCH_SETTINGS)


def _merge_settings(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged = deepcopy(dict(base))
    for key, value in overrides.items():
        normalized_key = str(key)
        existing = merged.get(normalized_key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[normalized_key] = _merge_settings(existing, value)
        else:
            merged[normalized_key] = deepcopy(value)
    return merged


def get_effective_research_settings(raw_settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    settings = default_research_settings()
    if raw_settings is None:
        from forven.db import kv_get

        try:
            raw_settings = kv_get("forven:settings", {})
        except Exception:
            raw_settings = {}
    if not isinstance(raw_settings, Mapping):
        return settings
    raw_research_settings = raw_settings.get("research_settings")
    if not isinstance(raw_research_settings, Mapping):
        return settings
    return _merge_settings(settings, raw_research_settings)


def get_research_sources_block(raw_settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the research_sources sub-block from effective settings (merged with defaults)."""
    effective = get_effective_research_settings(raw_settings)
    block = effective.get("research_sources")
    if not isinstance(block, Mapping):
        return {}
    return dict(block)


def _build_weighted_lane_order(lane_weights: Mapping[str, Any]) -> list[str]:
    weighted_order: list[str] = []
    for lane in _LANE_ORDER:
        weight = float(lane_weights.get(lane, 0.0) or 0.0)
        if weight <= 0:
            continue
        weighted_order.extend([lane] * max(1, int(round(weight * 10))))
    return weighted_order


def choose_research_lane(*, settings: Mapping[str, Any], cycle_index: int) -> str:
    lane_weights = settings.get("lane_weights")
    if isinstance(lane_weights, Mapping):
        weighted_order = _build_weighted_lane_order(lane_weights)
        if not weighted_order:
            weighted_order = _build_weighted_lane_order(_DEFAULT_RESEARCH_SETTINGS["lane_weights"])
        if weighted_order:
            return weighted_order[cycle_index % len(weighted_order)]
    return _LANE_ORDER[cycle_index % len(_LANE_ORDER)]


def _novelty_threshold_for_lane(lane: str) -> float:
    if lane == "exploration":
        return 0.65
    if lane == "benchmarking":
        return 0.35
    return 0.45


def build_research_contract(
    *,
    lane: str,
    settings: Mapping[str, Any],
    available_datasets: Sequence[str],
) -> ResearchContract:
    normalized_lane = str(lane or "").strip().lower()
    if normalized_lane not in _LANE_ORDER:
        raise ValueError(f"unknown research lane: {lane}")

    memory_modes = settings.get("memory_modes")
    if not isinstance(memory_modes, Mapping):
        memory_modes = _DEFAULT_RESEARCH_SETTINGS["memory_modes"]
    lane_memory_defaults = dict(_DEFAULT_RESEARCH_SETTINGS["memory_modes"][normalized_lane])
    lane_memory_mode = memory_modes.get(normalized_lane)
    if not isinstance(lane_memory_mode, Mapping):
        lane_memory_mode = lane_memory_defaults
    else:
        lane_memory_defaults.update({str(key): value for key, value in lane_memory_mode.items()})
        lane_memory_mode = lane_memory_defaults

    spawn_limits = settings.get("spawn_limits")
    if not isinstance(spawn_limits, Mapping):
        spawn_limits = _DEFAULT_RESEARCH_SETTINGS["spawn_limits"]

    allowed_external_source_types = settings.get("allowed_external_source_types")
    if not isinstance(allowed_external_source_types, Sequence) or isinstance(allowed_external_source_types, (str, bytes)):
        allowed_external_source_types = _DEFAULT_RESEARCH_SETTINGS["allowed_external_source_types"]

    external_benchmarking_enabled = bool(settings.get("external_benchmarking_enabled", False))
    external_sources_allowed = normalized_lane == "benchmarking" and external_benchmarking_enabled

    return ResearchContract(
        lane=normalized_lane,
        available_datasets=[str(dataset) for dataset in available_datasets],
        memory_mode=dict(lane_memory_mode),
        external_sources_allowed=external_sources_allowed,
        allowed_external_source_types=[str(source_type) for source_type in allowed_external_source_types],
        novelty_threshold=_novelty_threshold_for_lane(normalized_lane),
        spawn_limits={
            "per_run": int(spawn_limits.get("per_run", 2) or 2),
            "rolling_window": int(spawn_limits.get("rolling_window", 6) or 6),
            "window_days": int(spawn_limits.get("window_days", 7) or 7),
        },
    )
