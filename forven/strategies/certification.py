from __future__ import annotations

from dataclasses import dataclass

from forven.strategies.params import (
    ParamCanonicalizationMeta,
    canonicalize_params_with_metadata,
    is_known_runtime_type,
    validate_canonical_params,
)

EXECUTION_CERTIFIED_FAMILIES = frozenset(
    {
        "bb_fade",
        "bb_squeeze",
        "bollinger",
        "donchian",
        "ema_cross",
        "funding",
        "inside_bar",
        "keltner",
        "macd",
        "orb",
        "parabolic_sar",
        "rsi_momentum",
        "stochastic",
        "supertrend",
        "vwap_pullback",
        "regime_filtered",
        "williams_r",
    }
)


@dataclass(frozen=True)
class StrategyExecutionCertification:
    strategy_type: str
    family_type: str
    canonical_params: dict
    canonical_meta: ParamCanonicalizationMeta
    param_validation_errors: list[str]
    unregistered_runtime_type: bool = False

    @property
    def alias_resolutions(self) -> dict[str, str]:
        return dict(self.canonical_meta.alias_resolutions)

    @property
    def unknown_params(self) -> list[str]:
        return list(self.canonical_meta.unknown_params)

    @property
    def unsupported_rule_blobs(self) -> list[str]:
        return list(self.canonical_meta.unsupported_rule_blobs)

    @property
    def certified(self) -> bool:
        return (
            not self.unsupported_rule_blobs
            and not self.param_validation_errors
            and not self.unregistered_runtime_type
        )

    def primary_blocking_reason(self) -> str | None:
        if self.unregistered_runtime_type:
            return (
                f"no runtime class registered for strategy type '{self.strategy_type}' "
                f"(resolved family '{self.family_type}'; no class with this TYPE_NAME "
                "exists in the registry — register a strategy file that implements it, "
                "or use a type backed by a registered class)"
            )
        if self.unsupported_rule_blobs:
            return "unsupported rule-blob params: " + ", ".join(self.unsupported_rule_blobs)
        if self.param_validation_errors:
            return "invalid parameter values: " + "; ".join(self.param_validation_errors)
        return None

    def format_error(self, *, context: str) -> str | None:
        reason = self.primary_blocking_reason()
        if reason is None:
            return None
        strategy_label = self.strategy_type or self.family_type or "strategy"
        if context == "backtest":
            return (
                "Backtesting is restricted to strategies that can execute in paper/live. "
                f"Rejected {strategy_label}: {reason}."
            )
        if context == "creation":
            return (
                "Strategy creation is restricted to execution-certified strategies so anything "
                f"you backtest can also be traded. Rejected {strategy_label}: {reason}."
            )
        if context == "params":
            return f"Strategy parameters are invalid for {strategy_label}: {reason}."
        return reason


def certify_execution_strategy(
    strategy_type: str | None,
    raw_params: dict | None,
) -> StrategyExecutionCertification:
    normalized_type = str(strategy_type or "").strip()
    canonical_params, canonical_meta = canonicalize_params_with_metadata(
        normalized_type,
        raw_params,
    )
    # Only flag as unregistered when the caller actually supplied a type.
    # Empty strings arrive from intake/inference paths and are handled by
    # other error codes upstream.
    #
    # PHANTOM-1: certification requires a CONCRETE runtime class for the exact
    # resolved type (require_runtime_class=True, fail-closed on registry
    # errors). The old family-membership safety net is gone: it certified
    # class-less families ('vwap', 'regime_filtered') and — combined with the
    # fail-open registry probe — arbitrary made-up types (S05847
    # soad_shock_reversal), minting phantom strategies that occupy quick_screen
    # slots, burn gauntlet compute, and can never trade.
    unregistered = bool(normalized_type) and not is_known_runtime_type(
        normalized_type,
        require_runtime_class=True,
    )
    return StrategyExecutionCertification(
        strategy_type=normalized_type,
        family_type=canonical_meta.family_type,
        canonical_params=canonical_params,
        canonical_meta=canonical_meta,
        param_validation_errors=validate_canonical_params(
            canonical_meta.family_type,
            canonical_params,
        ),
        unregistered_runtime_type=unregistered,
    )


FAILURE_TIERS: dict[str, int] = {
    # Tier 1 — likely fixable with param tweak, sweep these first
    "backtest_insufficient_trades": 1,
    "param_out_of_range": 1,
    "adx_filter_mismatch": 1,
    # Tier 2 — structural issues, lower recovery priority
    "code_error": 2,
    "pattern_invalid": 2,
    # A future-bar leak is a structural quarantine, not a param-tweak fix; give it
    # a stable canonical key so a lookahead rejection classifies deterministically
    # (the raw probe reason otherwise defaults to tier 2 with the whole message as
    # the canonical, producing an unbounded status_reason).
    "lookahead": 2,
}


def classify_failure_tier(reason: str | None) -> tuple[int, str]:
    """Return (tier, canonical_reason) for a certification/stage failure reason.

    Unknown reasons default to tier 2.
    """
    if not reason:
        return 2, "unknown"
    lower = reason.strip().lower()
    for key, tier in FAILURE_TIERS.items():
        if key in lower:
            return tier, key
    return 2, lower


def resolve_initial_stage(certification: StrategyExecutionCertification) -> str:
    """Determine the correct initial stage for a new strategy container.

    Returns ``"quick_screen"`` if the certification passed, ``"research_only"``
    otherwise.  Strategies should never be created directly at gauntlet.
    """
    return "quick_screen" if certification.certified else "research_only"


__all__ = [
    "EXECUTION_CERTIFIED_FAMILIES",
    "FAILURE_TIERS",
    "StrategyExecutionCertification",
    "certify_execution_strategy",
    "classify_failure_tier",
    "resolve_initial_stage",
]
