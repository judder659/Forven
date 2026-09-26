from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


_DEFAULT_RESEARCH_SETTINGS: dict[str, Any] = {
    "external_benchmarking_enabled": True,
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
    # Strategy creation (forven.strategy_creation): autonomous idea-to-strategy
    # tasks queued per UTC day, and creation tasks allowed in flight at once.
    "strategy_creation_daily_budget": 40,
    "strategy_creation_max_in_flight": 2,
    # A new agent candidate whose input feeds cover less than this percent of the
    # quick-screen window cannot be fairly tested there (a recently collected feed
    # is "present" but silent for most of the window). It is archived
    # untestable:insufficient_history at registration. 0 disables.
    "candidate_min_feed_coverage_pct": 50,
    # Research holdout (forven.research_holdout): recent data research never sees.
    # Off by default. When on, research reads stop at the cutoff and each new
    # candidate gets one test on the held-back period before the paper gate.
    #   roll: "quarterly" (cutoff = current quarter start - lag_quarters) or
    #     "manual" (cutoff below). established_at is stamped when first enabled;
    #     strategies created earlier saw the held-back data and are exempt.
    #   paper_mode: off | observe | enforce (block paper promotion without a pass).
    #   max_family_shots: held-back tests per strategy family per cutoff (0 = no cap).
    "research_holdout": {
        "enabled": False,
        "roll": "quarterly",
        "lag_quarters": 2,
        "cutoff": "",
        "established_at": "",
        "paper_mode": "enforce",
        "min_trades": 5,
        "max_family_shots": 3,
    },
}


_LANE_ORDER = ("exploration", "exploitation", "benchmarking")

# Memory each research lane carries into a research task's context. Constraint
# memory is always on; inspiration memory (lessons, learned quant skills) too.
_MEMORY_MODES: dict[str, dict[str, Any]] = {
    "exploration": {"constraint_memory": True, "inspiration_memory": "optional"},
    "exploitation": {"constraint_memory": True, "inspiration_memory": "bounded"},
    "benchmarking": {"constraint_memory": True, "inspiration_memory": "bounded"},
}

# Settings blocks of the retired crucible loop. Stored copies are ignored when
# settings are read; its feed-coverage key moved to the top level.
_RETIRED_KEYS = ("hypothesis_discipline", "lane_weights", "spawn_limits", "memory_modes", "autonomous_discovery")


@dataclass(frozen=True, slots=True)
class ResearchContract:
    lane: str
    available_datasets: list[str]
    memory_mode: dict[str, Any]
    external_sources_allowed: bool
    allowed_external_source_types: list[str]
    novelty_threshold: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "available_datasets": list(self.available_datasets),
            "memory_mode": dict(self.memory_mode),
            "external_sources_allowed": self.external_sources_allowed,
            "allowed_external_source_types": list(self.allowed_external_source_types),
            "novelty_threshold": self.novelty_threshold,
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


def drop_retired_research_keys(merged: dict[str, Any], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Remove the retired crucible blocks, keeping the feed-coverage key they held."""
    legacy = raw.get("hypothesis_discipline")
    if (
        "candidate_min_feed_coverage_pct" not in raw
        and isinstance(legacy, Mapping)
        and legacy.get("candidate_min_feed_coverage_pct") is not None
    ):
        merged["candidate_min_feed_coverage_pct"] = legacy["candidate_min_feed_coverage_pct"]
    for key in _RETIRED_KEYS:
        merged.pop(key, None)
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
    return drop_retired_research_keys(_merge_settings(settings, raw_research_settings), raw_research_settings)


def research_read_cutoff() -> Any:
    """Where research candle reads stop (``forven.research_holdout``), or None."""
    from forven.research_holdout import normalize_settings, read_cutoff

    try:
        return read_cutoff(normalize_settings(get_effective_research_settings().get("research_holdout")))
    except Exception:
        return None


def get_research_sources_block(raw_settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the research_sources sub-block from effective settings (merged with defaults)."""
    effective = get_effective_research_settings(raw_settings)
    block = effective.get("research_sources")
    if not isinstance(block, Mapping):
        return {}
    return dict(block)


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

    allowed_external_source_types = settings.get("allowed_external_source_types")
    if not isinstance(allowed_external_source_types, Sequence) or isinstance(allowed_external_source_types, (str, bytes)):
        allowed_external_source_types = _DEFAULT_RESEARCH_SETTINGS["allowed_external_source_types"]

    external_benchmarking_enabled = bool(settings.get("external_benchmarking_enabled", False))
    external_sources_allowed = normalized_lane == "benchmarking" and external_benchmarking_enabled

    return ResearchContract(
        lane=normalized_lane,
        available_datasets=[str(dataset) for dataset in available_datasets],
        memory_mode=dict(_MEMORY_MODES[normalized_lane]),
        external_sources_allowed=external_sources_allowed,
        allowed_external_source_types=[str(source_type) for source_type in allowed_external_source_types],
        novelty_threshold=_novelty_threshold_for_lane(normalized_lane),
    )
