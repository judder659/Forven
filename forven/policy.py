"""Single source of truth for all pipeline promotion, demotion, and gate policies."""

import copy
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from statistics import mean, pstdev
from typing import Any

from forven.db import create_approval, get_db, kv_get, kv_set, log_activity, log_gate_rejection

from forven.gauntlet.legitimacy import validate_robustness_payload
from forven.util import normalize_stage

log = logging.getLogger("forven.policy")

DEFAULT_PIPELINE_CONFIG = {
    # Active stance preset (relaxed | default | strict | custom). The Default preset
    # IS this DEFAULT_PIPELINE_CONFIG; relaxed/strict are deltas applied in
    # _apply_pipeline_preset. "custom" = per-knob KV overrides used as-is.
    "pipeline_preset": "default",
    "testing_mode": False,
    "quick_screen": {
        # NOTE: the quick-screen backtest WINDOW is not set here. Every automatic
        # backtest (quick-screen, gauntlet optimize/confirm, walk-forward, cost-stress,
        # evolution validation) shares ONE window: the app setting
        # `backtest_duration_days` (Settings > Lab > "Backtest window"). The old
        # quick_screen.lookback_days knob was read by nothing — removed so the config
        # stops implying a per-stage window that never existed.
        "min_total_return_pct": 0.0,
        "max_drawdown_pct": 0.30,
        "min_sharpe": 0.0,
        # P1-10: Overfitting guardrails (mandatory pre-checks before gauntlet)
        "min_is_sharpe": 0.0,         # Gate 1: IS Sharpe >= 0 (reject negative)
        "max_is_maxdd_pct": 0.30,    # Gate 2: IS MaxDD <= 30% (auto-reject exceeded)
        "max_is_oos_ratio": 3.0,      # Gate 3: IS/OOS ratio <= 3x (reject >3x inversion)
        # Gate 4 DISABLED (0.0): win-rate is not a quality signal — momentum/breakout
        # strategies win ~30-40% and profit on payoff ratio, so a 45% floor here
        # auto-rejected an entire legitimate family at the cheapest triage. PF (1.05),
        # IS Sharpe (>=0), drawdown and min-trades still screen. Tail-risk guard
        # (win>70% AND PF<1.5) in brain.py still catches martingale-style curves.
        "min_win_rate": 0.0,          # Gate 4: win-rate floor off (was 0.45)
        # S00552 PF floor enforced at BOTH IS and OOS in the quick-screen gate.
        # Default preset relaxes to 1.0 — keep only a "not a clear loser" screen at
        # the cheapest triage; the Strict preset restores 1.1. (M-15 2026-06-09.)
        "min_profit_factor": 1.0,
        # Guardrail floors that were previously HARDCODED fallbacks inside
        # brain._quick_screen_overfitting_guardrails (Gate5/Gate3). Now preset-driven.
        # Default preset relaxes the entry sample to 20 and DROPS the quick-screen
        # robustness floor to 0: the composite robustness score is EARNED inside the
        # gauntlet (MC/jitter/WFA), so a non-zero floor at quick-screen time is a
        # catch-22 that empties the funnel. Strict preset restores 30 / 40.
        "min_trades": 20,
        "min_robustness_score": 0,
        # Fitness-scorer scaling knobs (score_strategy). Previously hardcoded as
        # min_trades_limit=20 / min_pf_limit=1.3 — now wired so the fitness curve
        # is tunable. Defaults preserve the historical scaling exactly.
        "fitness_min_trades": 20,
        "fitness_min_profit_factor": 1.3,
        # IMPLAUSIBLE-METRICS REJECT (lookahead/data-leak guard). A real crypto
        # strategy on honest data does not reach Sharpe >= 5 or profit factor >= 8
        # — those are the signature of a future-bar leak (e.g. a `.shift(-1)`),
        # which makes BOTH the IS and OOS slices uniformly amazing and so slips
        # past the IS/OOS-gap overfit detector and the win-rate trap (PF too high).
        # A Sharpe pegged at the +/-10 backtest clamp (_MAX_ABS_RISK_RATIO) is
        # always rejected regardless of this ceiling.
        "max_plausible_sharpe": 5.0,
        "max_plausible_profit_factor": 8.0,
    },
    "gauntlet": {
        # NOTE: the optimize/confirm WINDOW = the global `backtest_duration_days`
        # setting (see quick_screen note above). The old gauntlet.optimization_years
        # knob was read by nothing — removed.
        # "strict live, achievable paper": the gauntlet->paper gate stays
        # reachable in adverse regimes (so the funnel is visibly active) while
        # still rejecting clear losers — min_total_return stays >= 0, PF >= 1.05,
        # and walk_forward is required. The paper->live gate (paper_trading.*)
        # carries the strict forward-edge floors.
        "min_robustness_score": 30,
        "min_total_return_pct": 0.0,
        "max_drawdown_pct": 0.30,
        "min_win_rate": 0.0,
        # Win-rate is OFF as a gauntlet hard gate by default (see quick_screen note);
        # flip on only if you specifically want a win-rate floor at this stage.
        "gauntlet_enforce_win_rate": False,
        "min_trades": 20,
        "min_sharpe": 0.1,
        # Hard IS-Sharpe sanity floor — was a hardcoded 0.3 auto-reject in
        # brain._gauntlet_entry_guardrails that no Settings knob could relax. Now
        # wired: Default 0.0 (reject only genuinely negative IS edge), Strict 0.3.
        "hard_min_is_sharpe": 0.0,
        # S00552 OOS profit-factor floor at the gauntlet->paper gate. 1.05 matches
        # the previously-hardcoded floor (M-15 2026-06-09 audit).
        "min_oos_profit_factor": 1.05,
        # Monte-Carlo 95th-percentile drawdown ceiling (S00552). Explicit default so
        # the ratio-normalizer (F3, now in _RATIO_THRESHOLD_PATHS) has a real fallback
        # instead of 0.0, and so it's a surfaced/wired Settings knob. The paper gate
        # additionally clamps it to <= 0.40 via _PAPER_GATE_FLOORS.
        "mc_max_dd_p95": 0.40,
        # Max minutes a gauntlet async result (optimization) may stay 'running'
        # before the step treats it as a zombie and re-submits — prevents a stuck
        # poll wedging the workflow forever (the step heartbeat hides it from
        # stale-step recovery).
        "async_result_max_age_minutes": 60,
        # P1-8: Minimum required gauntlet tests. Default preset requires the two
        # cheap overfitting probes (walk_forward = OOS consistency, param_jitter =
        # parameter stability); cost_stress is a strict-LIVE concern deferred to the
        # paper->live gate. Strict preset re-adds cost_stress here.
        "required_tests": ["walk_forward", "param_jitter"],
        # P1-9: Walk-forward hard-pass thresholds
        "wfa_max_degradation": 0.35,
        "wfa_min_oos_trades": 20,
        # OOS folds need only be non-negative to reach paper (achievable paper);
        # demonstrated forward edge is enforced later at the strict paper->live gate.
        "wfa_min_oos_sharpe": 0.0,
        "wfa_min_folds": 2,
        # IMPLAUSIBLE-METRICS REJECT (defense-in-depth at the gauntlet->paper gate;
        # the primary catch is in quick_screen). See the quick_screen note above.
        "max_plausible_sharpe": 5.0,
        "max_plausible_profit_factor": 8.0,
    },
    # P25-1: Walk-forward analysis configuration (versioned, explicit).
    "walk_forward": {
        "version": 1,
        "n_folds": 5,
        "in_sample_pct": 0.70,
        "max_bars": 50000,
        "min_oos_days_1h": 30,
        "fee_bps": 4.5,
        "slippage_bps": 2.0,
    },
    # P25-4: Robustness threshold calibration — stricter target bands.
    "robustness_thresholds": {
        "monte_carlo_percentile_min": 0.65,
        # Recalibrated 0.70 -> 0.60: at n~30 reruns a 70% pass-rate has ~8% standard
        # error, so 70% was statistically indistinguishable from 67% — not a
        # meaningful bar. 60% still requires majority stability.
        "param_jitter_pass_rate_min": 0.50,
        # Cost-stress max Sharpe degradation under 2x fees/slippage (live gate).
        # The edge must SURVIVE higher costs, not just clear an absolute floor.
        "cost_stress_max_degradation_pct": 60.0,
        # Minimum baseline trade count before a parameter-jitter sweep runs.
        # Below this, jittering can't measure sensitivity meaningfully and the
        # ~50 reruns just burn compute (degenerate 1-trade baselines hit the
        # 600s timeout). Plain integer (not a ratio/percent) — stored verbatim.
        "param_jitter_min_trades": 10,
        # Compute bounds for the parameter-jitter sweep — the heaviest robustness
        # step (N full-window backtests). Each rerun now spans the baseline's
        # ACTUAL window (capped) instead of a fixed 720-bar slice, so without
        # these the sweep can overrun the step timeout and wedge the gauntlet at
        # param_jitter. All three are wired (Settings > Lab).
        "param_jitter_max_iterations": 30,  # cap reruns regardless of caller (was an unbounded 50)
        "param_jitter_max_bars": 4380,  # per-rerun window cap (~6mo @1h); < cost_stress's 8760
        "param_jitter_deadline_seconds": 240,  # wall-clock safety net; 0 disables
        "cost_stress_min_sharpe": 0.3,
        "regime_split_profitable_min": 0.50,
        # DEPRECATED: the gauntlet gate now reads wfa_fold_pass_rate_min (below) as
        # the single fold-pass-rate floor. Retained only for back-compat with any
        # persisted KV payloads; no longer consulted by _evaluate_gauntlet_gate.
        "wfa_pass_rate_band": [0.30, 0.60],
        # Fraction of walk-forward OOS folds that must be positive for the WFA verdict to
        # count as passed at the capital gate. Wired so it can be loosened in adverse regimes.
        # 0.40 = 2/5 folds positive: achievable-paper while still requiring multi-fold
        # consistency (not a single lucky fold). The strict paper->live gate enforces edge.
        "wfa_fold_pass_rate_min": 0.33,
        # Minimum OOS trades a walk-forward fold must have to count toward the
        # fold pass-rate. Near-empty folds (a trend system sitting out a flat
        # window) are excluded from BOTH numerator and denominator so they can't
        # drag the pass rate into a false reject. Plain integer.
        "wfa_min_fold_trades": 5,
        # Deflated Sharpe Ratio — optimizer selection-bias guard (the suite's
        # overfitting blind spot: strategies are optimized before validation with
        # no untouched holdout). OBSERVE-FIRST: the DSR is always computed and
        # surfaced for inspection; the reject gate is OPT-IN (default off) so its
        # effect can be watched before it blocks anything. min_deflated_sharpe is a
        # probability in [0,1] (~0.95 = conventional significance).
        "deflated_sharpe_gate_enabled": False,
        "min_deflated_sharpe": 0.90,
        "deflated_sharpe_default_trials": 50,
    },
    "paper_trading": {
        "min_paper_days": 14,
        "min_closed_trades": 10,
        "min_total_return_pct": 0.0,
        "max_drawdown_pct": 0.15,
        # "strict live": enforce the full robustness battery (WFA degradation /
        # absolute OOS Sharpe / OOS trades, MC percentile, cost-stress survival,
        # regime consistency) at the paper->live capital gate. These are demoted to
        # advisory at the gauntlet->paper gate ("achievable paper").
        "live_strict_robustness_enabled": True,
        # S00152 paper->live guardrails (M-15 2026-06-09 audit: previously
        # hardcoded in _evaluate_paper_gate; defaults preserve the enforced
        # values exactly — this is the live-money gate).
        # Hard PF floor for live deployment (evaluated on OOS PF when present).
        "min_profit_factor_live": 1.5,
        # Forward-paper edge floors — define "winning" at the live-money gate.
        # The PF floor above proves HISTORICAL edge; these require the FORWARD
        # paper trades themselves to show real edge (not merely a positive
        # return). Enforced in _evaluate_paper_gate against compute_live_metrics()
        # which derives them from the actual paper PnLs. Set to 0 to disable a
        # floor. min_paper_sharpe is the per-trade t-stat (mean/stdev*sqrt(n)):
        # 1.0 is a meaningful directional floor; raise toward ~1.6-2.0 (the
        # conventional significance band) BEFORE enabling real-money graduation.
        "min_paper_sharpe": 1.0,
        "min_profit_factor_paper": 1.2,
        # PF below this (but >= the floor) passes with a 50% position-size reduction.
        "pf_position_reduction_threshold": 2.0,
        # OOS Sharpe may not exceed IS Sharpe by more than this ratio (OOS>>IS
        # signals a lucky/overfit OOS window).
        "max_oos_is_ratio": 1.5,
    },
    "live_graduated": {
        "allocation_schedule": [
            {"week_start": 1, "week_end": 2, "allocation_pct": 25},
            {"week_start": 3, "week_end": 4, "allocation_pct": 50},
            {"week_start": 5, "week_end": 999, "allocation_pct": 100},
        ],
        "decay_kill_switch_pct": 0.30,
    },
    # Absolute anti-bypass floors clamped onto the promotion gates. FULLY EDITABLE —
    # these bound how far a relaxed preset / custom config / automated caller can
    # soften the path, but the operator can change them from Settings. Set a floor to
    # 0 (or a *_max_* ceiling to 1.0) to remove that rail entirely. paper_entry keys
    # clamp the gauntlet->paper gate (no real capital); live_* clamp the paper->live
    # (real money) gate — loosen those with care.
    "safety_floors": {
        "min_trades": 3,
        "min_robustness_score": 0.0,
        "mc_max_dd_p95": 0.50,
        "wfa_fold_pass_rate_min": 0.20,
        "param_jitter_pass_rate_min": 0.30,
        "live_min_closed_trades": 3,
        "live_max_drawdown_pct": 0.25,
    },
}

# --- Stance presets -------------------------------------------------------------
# "achievable paper, strict live": presets relax the path TO paper (no real capital
# at risk there) while the paper->live gate (paper_trading.*) stays the real filter.
# The Default preset == DEFAULT_PIPELINE_CONFIG, so it carries no deltas. Relaxed and
# Strict override specific knobs. Anything a preset (or custom config) sets is still
# clamped by the absolute anti-bypass floors (_PAPER_GATE_FLOORS for ->paper, and the
# inline live floors in _evaluate_paper_gate), so no preset can admit a 0-trade or
# 80%-drawdown strategy.
PIPELINE_PRESETS = {
    "default": {},
    "relaxed": {
        "quick_screen": {"min_trades": 5, "min_robustness_score": 0, "min_profit_factor": 1.0},
        "gauntlet": {
            "min_trades": 5,
            "min_sharpe": 0.0,
            "hard_min_is_sharpe": 0.0,
            "min_robustness_score": 20,
            "required_tests": ["walk_forward"],
        },
        "robustness_thresholds": {"wfa_fold_pass_rate_min": 0.25, "param_jitter_pass_rate_min": 0.40},
        "paper_trading": {
            "min_closed_trades": 5,
            "min_paper_days": 7,
            "min_paper_sharpe": 0.0,
            "min_profit_factor_live": 1.2,
        },
    },
    "strict": {
        "quick_screen": {"min_trades": 30, "min_robustness_score": 40, "min_profit_factor": 1.1, "min_is_sharpe": 0.2},
        "gauntlet": {
            "min_trades": 30,
            "min_sharpe": 0.5,
            "hard_min_is_sharpe": 0.3,
            "min_robustness_score": 50,
            "required_tests": ["walk_forward", "param_jitter", "cost_stress"],
        },
        "robustness_thresholds": {"wfa_fold_pass_rate_min": 0.50, "param_jitter_pass_rate_min": 0.60},
        "paper_trading": {
            "min_closed_trades": 50,
            "min_paper_days": 21,
            "min_paper_sharpe": 1.0,
            "min_profit_factor_live": 1.5,
        },
    },
}


def _apply_pipeline_preset(merged: dict, preset_name: object) -> None:
    """Deep-merge a stance preset's deltas into ``merged`` in place (one level deep,
    matching _normalize_pipeline_config's own section merge). Unknown names and
    "default"/"custom" leave the base untouched so per-knob KV overrides win as-is."""
    name = str(preset_name or "default").strip().lower()
    deltas = PIPELINE_PRESETS.get(name)
    if not deltas:
        return
    for section, knobs in deltas.items():
        if isinstance(knobs, dict) and isinstance(merged.get(section), dict):
            merged[section].update(knobs)
        else:
            merged[section] = knobs


_GAUNTLET_VERDICT_ALIASES = {
    "parameter_stability": "param_jitter",
    "parameter_jitter": "param_jitter",
    "regime_performance": "regime_split",
}

_GAUNTLET_VALIDATION_TYPES = ["walk_forward", "monte_carlo", "param_jitter", "cost_stress", "regime_split"]

_GAUNTLET_SUCCESS_STATUSES = {"succeeded", "success", "pass", "passed", "done", "completed", "complete", "ok"}
_GAUNTLET_FAILURE_STATUSES = {
    "fail",
    "failed",
    "failure",
    "error",
    "errored",
    "cancelled",
    "canceled",
    "blocked",
    "rejected",
}


_RATIO_THRESHOLD_PATHS = (
    ("quick_screen", "max_drawdown_pct"),
    ("gauntlet", "max_drawdown_pct"),
    ("paper_trading", "max_drawdown_pct"),
    ("live_graduated", "decay_kill_switch_pct"),
    # Fraction-of-folds floor: operators reasonably enter 60 (percent) where the
    # config canonically stores 0.60; without normalization a raw 60 makes the
    # WFA fold gate unsatisfiable (pass_rate <= 1.0 < 60).
    ("robustness_thresholds", "wfa_fold_pass_rate_min"),
    # F3 (2026-06-15): surfaced in Settings with %-based UX — normalize so operators
    # may enter 40 / 0.40 (or 60 / 0.60) interchangeably for the gauntlet->paper gate.
    ("gauntlet", "mc_max_dd_p95"),
    ("robustness_thresholds", "param_jitter_pass_rate_min"),
    # safety_floors ratio rails — accept percent (40) or fraction (0.40) like the gate
    # twins above, so a percent-habit entry can't store a raw 40 and silently no-op a
    # rail (incl. the real-money live_max_drawdown_pct ceiling).
    ("safety_floors", "mc_max_dd_p95"),
    ("safety_floors", "wfa_fold_pass_rate_min"),
    ("safety_floors", "param_jitter_pass_rate_min"),
    ("safety_floors", "live_max_drawdown_pct"),
    # Legacy keys preserved for backward compatibility.
    ("paper_gate", "max_drawdown_pct"),
    ("retirement", "max_drawdown_pct"),
    ("decay", "degradation_threshold"),
)


def _coerce_ratio_threshold(value: object, default: float) -> float:
    """Accept either fraction (0.40) or percent points (40) and return fraction."""
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)

    if parsed < 0:
        parsed = 0.0
    if parsed > 1.0:
        parsed = parsed / 100.0
    if parsed > 1.0:
        parsed = 1.0
    return float(parsed)


# Ratio thresholds that the settings UI presents with a "%" unit. The policy
# config canonically stores these as fractions (0.30); the UI contract is whole
# percent (30). Conversion happens exactly once, at the settings read boundary.
_UI_PERCENT_THRESHOLD_PATHS = (
    ("quick_screen", "max_drawdown_pct"),
    ("gauntlet", "max_drawdown_pct"),
    ("paper_trading", "max_drawdown_pct"),
    ("live_graduated", "decay_kill_switch_pct"),
)


def pipeline_thresholds_for_display(config: dict) -> dict:
    """Deep-copy ``config`` with ratio thresholds expressed as whole percent.

    ``load_pipeline_config`` always returns these fields as normalized
    fractions in [0, 1], so the conversion here is exact (no heuristics):
    0.30 -> 30. Writes from the UI in whole percent round-trip through
    ``_coerce_ratio_threshold`` on the next load.
    """
    out = copy.deepcopy(config) if isinstance(config, dict) else {}
    for section, field in _UI_PERCENT_THRESHOLD_PATHS:
        payload = out.get(section)
        if not isinstance(payload, dict) or field not in payload:
            continue
        try:
            value = float(payload[field])
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 1.0:
            payload[field] = round(value * 100.0, 2)
    return out


def _normalize_pipeline_config(config: dict | None) -> dict:
    """Merge defaults and normalize ratio-based thresholds to fractions."""
    merged = copy.deepcopy(DEFAULT_PIPELINE_CONFIG)
    raw = config if isinstance(config, dict) else {}

    # Apply the stance preset over DEFAULT first (handles a sparse config that carries
    # only pipeline_preset, e.g. a fresh selection with no materialized knobs).
    _apply_pipeline_preset(merged, raw.get("pipeline_preset"))

    for key, val in raw.items():
        if isinstance(val, dict) and key in merged and isinstance(merged[key], dict):
            merged[key].update(val)
        else:
            merged[key] = val

    # Ensure testing_mode is boolean
    if "testing_mode" in merged:
        val = merged["testing_mode"]
        if isinstance(val, str):
            merged["testing_mode"] = val.lower() in ("true", "1", "yes", "on")
        else:
            merged["testing_mode"] = bool(val)

    # Legacy compatibility: map old gate payloads into the new 4-step schema.
    legacy_paper_gate = raw.get("paper_gate") if isinstance(raw.get("paper_gate"), dict) else {}
    legacy_deploy_gate = raw.get("deploy_gate") if isinstance(raw.get("deploy_gate"), dict) else {}
    legacy_retirement = raw.get("retirement") if isinstance(raw.get("retirement"), dict) else {}
    legacy_decay = raw.get("decay") if isinstance(raw.get("decay"), dict) else {}

    quick_screen = merged.get("quick_screen", {})
    if not isinstance(quick_screen, dict):
        quick_screen = {}
        merged["quick_screen"] = quick_screen
    if legacy_paper_gate:
        if "min_sharpe" in legacy_paper_gate:
            quick_screen["min_sharpe"] = legacy_paper_gate.get("min_sharpe")
        if "max_drawdown_pct" in legacy_paper_gate:
            quick_screen["max_drawdown_pct"] = legacy_paper_gate.get("max_drawdown_pct")

    paper_trading = merged.get("paper_trading", {})
    if not isinstance(paper_trading, dict):
        paper_trading = {}
        merged["paper_trading"] = paper_trading
    if legacy_deploy_gate:
        if "min_paper_days" in legacy_deploy_gate:
            paper_trading["min_paper_days"] = legacy_deploy_gate.get("min_paper_days")
        if "min_paper_trades" in legacy_deploy_gate:
            paper_trading["min_closed_trades"] = legacy_deploy_gate.get("min_paper_trades")
        if "min_total_return_pct" in legacy_deploy_gate:
            paper_trading["min_total_return_pct"] = legacy_deploy_gate.get("min_total_return_pct")

    explicit_paper_cfg = raw.get("paper_trading") if isinstance(raw.get("paper_trading"), dict) else {}
    if (
        legacy_retirement
        and "max_drawdown_pct" in legacy_retirement
        and "max_drawdown_pct" not in explicit_paper_cfg
    ):
        # Legacy retirement drawdown maps to the modern paper_trading drawdown gate.
        paper_trading["max_drawdown_pct"] = legacy_retirement.get("max_drawdown_pct")

    live_graduated = merged.get("live_graduated", {})
    if not isinstance(live_graduated, dict):
        live_graduated = {}
        merged["live_graduated"] = live_graduated
    if legacy_decay and "degradation_threshold" in legacy_decay:
        live_graduated["decay_kill_switch_pct"] = legacy_decay.get("degradation_threshold")

    # Named-preset AUTHORITY: a deliberately-chosen stance (relaxed/strict) must win
    # over a fully-materialized stored knob snapshot. load_pipeline_config self-heals
    # the KV to a complete dict (incl. the legacy deploy_gate alias) and the Settings
    # save round-trips the whole config, so the raw overlay AND the legacy back-mapping
    # above otherwise clobber the preset deltas and make the selector INERT (picking
    # Strict would store the stance but keep running the looser values). Re-apply the
    # deltas HERE — after the legacy paper_gate/deploy_gate mapping — so the preset
    # takes effect end-to-end (incl. the deploy_gate alias republished below).
    # 'default'/'custom'/absent leave the raw per-knob values winning, so manual edits
    # stick (the UI flips the selector to "custom" the moment a knob is edited).
    _preset_name = str(raw.get("pipeline_preset") or "").strip().lower()
    if _preset_name in ("relaxed", "strict"):
        _apply_pipeline_preset(merged, _preset_name)

    for section, field in _RATIO_THRESHOLD_PATHS:
        section_payload = merged.get(section, {})
        if not isinstance(section_payload, dict):
            section_payload = {}
            merged[section] = section_payload
        default_value = float(DEFAULT_PIPELINE_CONFIG.get(section, {}).get(field, 0.0))
        section_payload[field] = _coerce_ratio_threshold(section_payload.get(field), default_value)

    # Publish backward-compatible aliases so existing callers keep working.
    merged["paper_gate"] = {
        "min_sharpe": float(merged["quick_screen"].get("min_sharpe", 1.0)),
        "max_drawdown_pct": float(merged["quick_screen"].get("max_drawdown_pct", 0.25)),
        "min_profit_factor": 1.0,
        "min_trades": 5,
    }
    merged["deploy_gate"] = {
        "min_paper_trades": int(merged["paper_trading"].get("min_closed_trades", 10)),
        "min_total_return_pct": float(merged["paper_trading"].get("min_total_return_pct", 0.0)),
        "min_paper_days": int(merged["paper_trading"].get("min_paper_days", 14)),
        "min_fitness": 0,
    }
    merged["retirement"] = {"max_fitness": 0, "max_drawdown_pct": float(merged["paper_trading"].get("max_drawdown_pct", 0.25))}
    merged["decay"] = {
        "window_hours": 72,
        "degradation_threshold": float(merged["live_graduated"].get("decay_kill_switch_pct", 0.30)),
        "min_trades": 5,
    }

    # Guard: walk_forward is the core out-of-sample gate and must always be a
    # REQUIRED test. A config that drops it — e.g. a stale settings-save that
    # reverted to the soak-era required_tests=['monte_carlo'] — makes the strict
    # Monte-Carlo bootstrap the SOLE gate (65% bootstrap-profitable), which
    # starves graduation: strategies that pass walk_forward + cost_stress get
    # killed at monte_carlo and nothing reaches paper. Restore the launch default
    # whenever a NON-EMPTY required list lacks walk_forward (empty == "enforce
    # all", left intact). load_pipeline_config() then self-heals the KV.
    _gaunt = merged.get("gauntlet")
    if isinstance(_gaunt, dict) and isinstance(_gaunt.get("required_tests"), list) and _gaunt["required_tests"]:
        try:
            from forven.gauntlet.settings import normalize_required_tests

            _norm_req = set(normalize_required_tests(_gaunt["required_tests"]))
        except Exception:
            _norm_req = {str(x).strip().lower() for x in _gaunt["required_tests"]}
        if "walk_forward" not in _norm_req:
            log.warning(
                "pipeline config: required_tests %s lacks walk_forward (the OOS gate) — "
                "restoring launch default %s (Monte-Carlo-only gating starves graduation)",
                _gaunt["required_tests"],
                DEFAULT_PIPELINE_CONFIG["gauntlet"]["required_tests"],
            )
            _gaunt["required_tests"] = list(DEFAULT_PIPELINE_CONFIG["gauntlet"]["required_tests"])

    return merged


def load_pipeline_config() -> dict:
    """Load pipeline thresholds from KV store, fallback to defaults."""
    config = kv_get("forven:pipeline_thresholds")
    normalized = _normalize_pipeline_config(config if isinstance(config, dict) else None)

    # Heal legacy percent-point payloads in KV so all downstream consumers are consistent.
    if isinstance(config, dict) and config != normalized:
        kv_set("forven:pipeline_thresholds", normalized)
    return normalized

def save_pipeline_config(config: dict):
    """Save pipeline thresholds to KV store."""
    kv_set("forven:pipeline_thresholds", _normalize_pipeline_config(config))

def validate_backtest_metrics(metrics: dict) -> tuple[bool, float, str]:
    """
    Validate backtest metrics for overfitting guardrails.
    
    Checks:
    1. IS/OOS Sharpe gap - rejects strategies where in-sample Sharpe minus 
       out-of-sample Sharpe exceeds 0.5 points (prevents overfitted strategies)
    2. Robustness penalties for gauntlet gate:
       - IS/OOS gap > 0.20: penalize fitness by 20 points
       - MaxDD > 30%: penalize fitness by 15 points  
       - Trade count < 50: penalize fitness by 10 points
    
    Returns:
        tuple: (is_valid, fitness_score, rejection_reason)
        - is_valid: False if metrics fail validation
        - fitness_score: 0.0 if rejected, otherwise calculated fitness
        - rejection_reason: empty string if passed, otherwise describes the failure
    """
    rejection_reason = ""
    penalty = 0.0

    # MATH-02: reject statistically meaningless single-trade strategies.
    # Sharpe/Sortino with one trade have zero degrees of freedom and any
    # downstream comparison against a threshold is noise-driven.
    total_trades_for_check = int(metrics.get("total_trades", 0) or 0)
    if total_trades_for_check < 2:
        rejection_reason = f"Insufficient trade count: {total_trades_for_check} < 2 (Sharpe undefined)"
        log.warning(f"Strategy rejected: {rejection_reason}")
        return False, 0.0, rejection_reason

    # MATH-02: penalize <5 trades (statistically weak) before scoring.
    if total_trades_for_check < 5:
        penalty += 30.0
        rejection_reason = f"Trade count: {total_trades_for_check} < 5 (-30 pts, statistically weak)"

    # Check for IS/OOS Sharpe gap guardrail (hard reject at 0.5)
    is_sharpe = metrics.get("is_sharpe")
    oos_sharpe = metrics.get("oos_sharpe")
    
    if is_sharpe is not None and oos_sharpe is not None:
        gap = is_sharpe - oos_sharpe
        if gap > 1.5:
            rejection_reason = f"IS/OOS Sharpe gap too large: {gap:.2f} > 1.5 (is_sharpe={is_sharpe:.2f}, oos_sharpe={oos_sharpe:.2f})"
            log.warning(f"Strategy rejected: {rejection_reason}")
            return False, 0.0, rejection_reason
        
        # Robustness penalty: IS/OOS gap > 0.50
        if gap > 0.50:
            penalty += 20.0
            rejection_reason = f"IS/OOS gap penalty: {gap:.2f} > 0.50 (-20 pts)"
    
    # Robustness penalty: MaxDD > 50%
    max_dd = abs(metrics.get("max_drawdown_pct", 0))
    if max_dd > 0.50:
        penalty += 15.0
        rejection_reason = f"MaxDD penalty: {max_dd*100:.1f}% > 50% (-15 pts)" + (f"; {rejection_reason}" if rejection_reason else "")
    
    # Robustness penalty: Trade count < 20
    total_trades = metrics.get("total_trades", 0)
    if total_trades < 20:
        penalty += 10.0
        rejection_reason = f"Trade count penalty: {total_trades} < 20 (-10 pts)" + (f"; {rejection_reason}" if rejection_reason else "")
    
    if penalty > 0:
        log.warning(f"Robustness penalties applied: -{penalty:.1f} points. {rejection_reason}")
    
    return True, penalty, rejection_reason


def score_strategy(metrics: dict) -> float:
    """
    Compute fitness score (0-100) from backtest metrics.
    Consolidated from fitness.py. Scales dynamically based on user limits.
    
    Includes IS/OOS Sharpe gap guardrail to reject overfitted strategies.
    """
    if not metrics or metrics.get("total_trades", 0) < 1:
        return 0.0

    # Run validation guardrails (IS/OOS Sharpe gap check)
    is_valid, validation_penalty, rejection_reason = validate_backtest_metrics(metrics)
    if not is_valid:
        return validation_penalty
    # MATH-05: apply robustness penalty after computing the base fitness.
    # validate_backtest_metrics returns a non-negative penalty value when
    # the strategy is valid but has weak shape (gap, dd, low trade count).
    # Previously the value was discarded, so demoted strategies received
    # the same fitness as clean ones.

    pipeline = load_pipeline_config()
    quick_screen = pipeline.get("quick_screen", {})
    max_dd_limit = quick_screen.get("max_drawdown_pct", 0.25)
    # Launch hardening: these two were hardcoded (dead knobs) while max_dd and
    # sharpe limits were read from config. Now wired via quick_screen.fitness_*;
    # defaults preserve the historical 20 / 1.3 scaling exactly.
    min_trades_limit = int(quick_screen.get("fitness_min_trades", 20) or 20)
    min_pf_limit = float(quick_screen.get("fitness_min_profit_factor", 1.3) or 1.3)
    min_sharpe_limit = quick_screen.get("min_sharpe", 1.0)

    # Sharpe (30%) — scale: 0=0, maxes out at 2.5x the user's minimum
    sharpe = max(0, metrics.get("sharpe", 0))
    sharpe_cap = max(3.0, min_sharpe_limit * 2.5)
    sharpe_score = min(100, (sharpe / sharpe_cap) * 100) if sharpe_cap > 0 else 100

    # Win rate (20%) — direct percentage
    win_rate = metrics.get("win_rate", 0)
    win_score = win_rate * 100

    # Profit factor (20%) — scale: 1=0, maxes out at min_pf + 2.0
    pf = max(0, metrics.get("profit_factor", 0))
    if pf <= 1:
        pf_score = 0
    else:
        pf_cap_range = max(1.0, min_pf_limit + 2.0 - 1.0)
        pf_score = min(100, ((pf - 1) / pf_cap_range) * 100)

    # Max drawdown penalty (15%) — 100=no DD, 0=user's max DD limit
    max_dd = abs(metrics.get("max_drawdown_pct", 0))
    if max_dd_limit > 0:
        dd_score = max(0, min(100, (1 - max_dd / max_dd_limit) * 100))
    else:
        dd_score = 0 if max_dd > 0 else 100

    # Trade count bonus (15%) — min 1 trade, full score at user's min_trades
    total_trades = metrics.get("total_trades", 0)
    if min_trades_limit > 0:
        trade_score = max(0, min(100, (total_trades / min_trades_limit) * 100))
    else:
        trade_score = 100

    fitness = (
        sharpe_score * 0.30
        + win_score * 0.20
        + pf_score * 0.20
        + dd_score * 0.15
        + trade_score * 0.15
    )

    # MATH-05: subtract validation penalties (was previously discarded).
    if validation_penalty > 0:
        fitness = max(0.0, fitness - float(validation_penalty))

    return round(fitness, 1)

def _normalize_pipeline_stage(value: str | None) -> str:
    return normalize_stage(value)


def _to_percent_points(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    if abs(parsed) <= 1.0:
        return parsed * 100.0
    return parsed


def _to_ratio(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = float(default)
    if parsed > 1.0:
        return parsed / 100.0
    return parsed


def _coerce_optional_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return float(parsed)


def _coerce_float(value: object, default: float = 0.0) -> float:
    parsed = _coerce_optional_float(value)
    if parsed is None:
        return float(default)
    return float(parsed)


def _resolve_robustness_points(metrics: dict) -> float:
    """Resolve a strategy's robustness score on a 0-100 scale, honoring a real 0.0.

    Uses explicit None-coalescing (not a truthy ``or``-chain) so a legitimately
    recomputed composite of 0.0 is NOT skipped in favour of a stale 0-1 ``robustness``
    value left over from a prior (better) run. ``composite_robustness_score`` /
    ``robustness_score`` are stored on a 0-100 scale; the legacy ``robustness`` key is
    0-1, so a value with magnitude <= 1.0 is rescaled to 0-100.
    """
    raw: object = None
    for key in ("composite_robustness_score", "robustness_score", "robustness", "gauntlet_score"):
        val = metrics.get(key)
        if val is not None:
            raw = val
            break
    robustness = _coerce_float(raw, 0.0)
    if abs(robustness) <= 1.0:
        robustness = robustness * 100.0
    return robustness


def _parse_json_blob(value: object, default: object):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except Exception:
        return default


def _load_pipeline_settings() -> dict:
    """Load pipeline settings from KV store for readiness gate checks."""
    raw = kv_get("forven:pipeline:settings")
    if not isinstance(raw, dict):
        raw = {}
    # Merge with defaults so new keys are always present
    from forven.api_core import _DEFAULT_PIPELINE_SETTINGS
    merged = dict(_DEFAULT_PIPELINE_SETTINGS)
    merged.update(raw)
    return merged


# ---------------------------------------------------------------------------
# Promotion Readiness Gate Checks
# ---------------------------------------------------------------------------

def _check_multi_tf_backtests(strategy_id: str, min_tfs: int = 3) -> tuple[bool, str, list[str]]:
    """Verify backtests exist across at least ``min_tfs`` distinct timeframes."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT DISTINCT LOWER(TRIM(timeframe)) AS tf
               FROM backtest_results
               WHERE strategy_id = ?
                 AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                 AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')""",
            (strategy_id,),
        ).fetchall()
    timeframes = [r["tf"] for r in rows if r["tf"]]
    if len(timeframes) < min_tfs:
        return (
            False,
            f"Multi-TF sweep incomplete: {len(timeframes)}/{min_tfs} timeframes tested ({', '.join(timeframes) or 'none'})",
            timeframes,
        )
    return True, f"Multi-TF sweep passed: {len(timeframes)} timeframes tested", timeframes


def _check_optimization_exists(strategy_id: str) -> tuple[bool, str]:
    """Verify at least one optimization run exists in backtest_results."""
    with get_db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS cnt FROM backtest_results
               WHERE strategy_id = ?
                 AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'optimization'
                 AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')""",
            (strategy_id,),
        ).fetchone()
    count = int(row["cnt"] or 0) if row else 0
    if count <= 0:
        return False, "No optimization runs found — run parameter optimization first"
    return True, f"Optimization evidence found ({count} run{'s' if count > 1 else ''})"


def _check_params_applied(strategy_id: str) -> tuple[bool, str]:
    """Verify strategy params match the best optimization result's best_params."""
    with get_db() as conn:
        strat_row = conn.execute(
            "SELECT params FROM strategies WHERE id = ?", (strategy_id,),
        ).fetchone()
        opt_row = conn.execute(
            """SELECT metrics_json, config_json FROM backtest_results
               WHERE strategy_id = ?
                 AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'optimization'
                 AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
               ORDER BY datetime(created_at) DESC LIMIT 1""",
            (strategy_id,),
        ).fetchone()

    if not opt_row:
        return False, "No optimization result to compare params against"

    # Parse strategy params
    raw_params = strat_row["params"] if strat_row else "{}"
    try:
        current_params = json.loads(raw_params) if isinstance(raw_params, str) else (raw_params or {})
    except Exception:
        current_params = {}

    # Parse optimization best_params — the optimized params can be stored as:
    #   config_json.best_params  (gauntlet pipeline path)
    #   config_json.params       (post_optimization_submit path)
    #   metrics_json.best_params (legacy)
    best_params = {}
    for col in ("config_json", "metrics_json"):
        raw_opt = opt_row[col] if opt_row else "{}"
        try:
            parsed = json.loads(raw_opt) if isinstance(raw_opt, str) else (raw_opt or {})
        except Exception:
            parsed = {}
        for key in ("best_params", "params"):
            candidate = parsed.get(key, {})
            if isinstance(candidate, dict) and candidate:
                best_params = candidate
                break
        if best_params:
            break
    if not best_params:
        return False, "Optimization result has no best_params — re-run optimization"

    if not isinstance(current_params, dict) or not isinstance(best_params, dict):
        return False, "Cannot compare params — unexpected format"

    # Check that all best_params keys are present and match in strategy params
    mismatched = []
    for key, expected_val in best_params.items():
        actual_val = current_params.get(key)
        if actual_val != expected_val:
            try:
                if abs(float(actual_val) - float(expected_val)) < 1e-9:
                    continue
            except (TypeError, ValueError):
                pass
            mismatched.append(key)

    if mismatched:
        return (
            False,
            f"Optimized params not applied — mismatched keys: {', '.join(mismatched[:5])}. "
            f"Apply best params from optimization before promotion.",
        )
    return True, "Strategy params match optimization best params"


def _check_confirmation_backtest(strategy_id: str) -> tuple[bool, str]:
    """Verify a full backtest exists that was run AFTER the latest optimization.

    Includes auto-trashed backtests so that a manually-run confirmation
    backtest that was soft-deleted by quality gates still counts.
    """
    with get_db() as conn:
        opt_row = conn.execute(
            """SELECT created_at FROM backtest_results
               WHERE strategy_id = ?
                 AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'optimization'
                 AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
               ORDER BY datetime(created_at) DESC LIMIT 1""",
            (strategy_id,),
        ).fetchone()
        if not opt_row:
            return False, "No optimization found — cannot verify confirmation backtest"

        opt_time = str(opt_row["created_at"] or "")
        # Include trashed backtests — auto-trash quality gates should not
        # invalidate the fact that a confirmation backtest was run.
        bt_row = conn.execute(
            """SELECT result_id, created_at, deleted_at FROM backtest_results
               WHERE strategy_id = ?
                 AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'backtest'
                 AND created_at > ?
               ORDER BY datetime(created_at) DESC LIMIT 1""",
            (strategy_id, opt_time),
        ).fetchone()

    if not bt_row:
        return (
            False,
            "No confirmation backtest after optimization — run a full backtest with optimized params",
        )
    return True, f"Confirmation backtest found ({bt_row['result_id']})"


def _check_artifact_ordering(strategy_id: str, required_types: list[str] | None = None) -> tuple[bool, str]:
    """Verify artifacts were created in the correct order:
    multi-TF backtests → optimization → confirmation backtest → validation tests.
    """
    with get_db() as conn:
        rows = conn.execute(
            """SELECT LOWER(TRIM(COALESCE(result_type, 'backtest'))) AS rt,
                      MAX(datetime(created_at)) AS latest
               FROM backtest_results
               WHERE strategy_id = ?
                 AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
               GROUP BY LOWER(TRIM(COALESCE(result_type, 'backtest')))""",
            (strategy_id,),
        ).fetchall()

    timestamps: dict[str, str] = {}
    for row in rows:
        rt = str(row["rt"] or "")
        timestamps[rt] = str(row["latest"] or "")

    opt_time = timestamps.get("optimization", "")

    # Check ordering: optimization before validation tests
    required = {
        _canonicalize_gauntlet_verdict_test(rt)
        for rt in (required_types or [])
        if str(rt or "").strip()
    }
    validation_types = sorted(required) if required else list(_GAUNTLET_VALIDATION_TYPES)
    for vt in validation_types:
        vt_time = timestamps.get(vt, "")
        if vt_time and opt_time and vt_time < opt_time:
            return False, f"Ordering violation: {vt} was run before optimization — re-run after optimization"

    return True, "Artifact ordering is correct"


def _check_validation_freshness(strategy_id: str, required_types: list[str] | None = None) -> tuple[bool, str]:
    """Verify that validation tests were run after the latest optimization.

    The baseline is the latest optimization timestamp only.  Previously this
    also used ``strategy.updated_at``, but that column is bumped by many
    non-param operations (stage transitions, name changes, saves without
    actual param edits), which caused fresh validation tests to appear stale.
    """
    with get_db() as conn:
        opt_row = conn.execute(
            """SELECT created_at FROM backtest_results
               WHERE strategy_id = ?
                 AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = 'optimization'
                 AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
               ORDER BY datetime(created_at) DESC LIMIT 1""",
            (strategy_id,),
        ).fetchone()

    baseline = str(opt_row["created_at"] or "") if opt_row else ""

    if not baseline:
        return True, "No optimization found — freshness check skipped"

    required = {
        _canonicalize_gauntlet_verdict_test(rt)
        for rt in (required_types or [])
        if str(rt or "").strip()
    }
    validation_types = sorted(required) if required else list(_GAUNTLET_VALIDATION_TYPES)
    stale = []
    with get_db() as conn:
        for vt in validation_types:
            row = conn.execute(
                """SELECT created_at FROM backtest_results
                   WHERE strategy_id = ?
                     AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) = ?
                     AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
                   ORDER BY datetime(created_at) DESC LIMIT 1""",
                (strategy_id, vt),
            ).fetchone()
            if row and str(row["created_at"] or "") < baseline:
                stale.append(vt)

    if stale:
        return (
            False,
            f"Stale validation tests (run before latest optimization): {', '.join(stale)}",
        )
    return True, "All validation tests are fresh"


def _check_artifact_rows_exist(strategy_id: str, required_types: list[str]) -> tuple[bool, str]:
    """Verify each required validation has a persisted, passing verdict payload."""
    required = {_canonicalize_gauntlet_verdict_test(rt) for rt in required_types if str(rt or "").strip()}
    payloads, _overall = _extract_gauntlet_verdict_payloads(strategy_id, None, {})
    passing = {
        test_name
        for test_name, payload in payloads.items()
        if test_name in required and isinstance(payload, dict) and not _verdict_payload_failed(payload)
    }

    missing = sorted(required - passing)
    if missing:
        return (
            False,
            f"Missing passing persisted artifact rows for: {', '.join(missing)}. "
            f"Run or rerun these tests until the saved verdicts pass.",
        )
    return True, f"All required artifact rows passed: {', '.join(sorted(passing))}"


def check_promotion_readiness(strategy_id: str) -> dict:
    """Build a full readiness checklist for promoting a strategy to paper trading.

    Returns a dict with ``ready`` (bool) and ``steps`` (list of check results).
    Each step has: name, status ('passed'|'failed'|'skipped'|'warning'), detail, actionable.
    """
    ps = _load_pipeline_settings()
    steps: list[dict] = []

    def _run_check(name: str, enabled_key: str, required_key: str, check_fn, *args):
        enabled = ps.get(enabled_key, True)
        required = ps.get(required_key, True)
        if not enabled:
            steps.append({"name": name, "status": "skipped", "detail": "Disabled in settings",
                          "actionable": None})
            return
        ok, detail, *extra = check_fn(*args)
        if ok:
            steps.append({"name": name, "status": "passed", "detail": detail,
                          "actionable": None, "extra": extra[0] if extra else None})
        elif required:
            steps.append({"name": name, "status": "failed", "detail": detail,
                          "actionable": _action_for_check(name), "extra": extra[0] if extra else None})
        else:
            steps.append({"name": name, "status": "warning", "detail": detail,
                          "actionable": _action_for_check(name), "extra": extra[0] if extra else None})

    # 1. Multi-TF backtest sweep
    min_tfs = int(ps.get("gate_multi_tf_min_timeframes", 3))
    _run_check("multi_tf_sweep", "gate_multi_tf_sweep_enabled", "gate_multi_tf_sweep_required",
               _check_multi_tf_backtests, strategy_id, min_tfs)

    # 2. Validation tests (WFA, MC, jitter, cost, regime)
    config = load_pipeline_config()
    gauntlet_cfg = config.get("gauntlet", {})
    required_tests = gauntlet_cfg.get("required_tests", [])
    if required_tests:
        _run_check("validation_artifacts", "gate_require_artifact_rows_enabled",
                   "gate_require_artifact_rows_required",
                   _check_artifact_rows_exist, strategy_id, required_tests)

    ready = all(s["status"] in ("passed", "skipped", "warning") for s in steps)
    return {"ready": ready, "steps": steps, "strategy_id": strategy_id}


def _action_for_check(name: str) -> str | None:
    """Return an actionable hint for each check so the frontend can offer a button."""
    actions = {
        "multi_tf_sweep": "run_timeframe_sweep",
        "validation_artifacts": "run_validation_suite",
        # Paper-live optimization steps
        "optimization": "run_optimization",
        "params_applied": "apply_best_params",
        "confirmation_backtest": "run_confirmation_backtest",
    }
    return actions.get(name)


def check_paper_live_readiness(strategy_id: str) -> dict:
    """Build a readiness checklist for promoting a strategy from paper to live.

    Returns the same shape as ``check_promotion_readiness``: ``{ready, steps, strategy_id}``.
    Steps cover paper trading metrics (informational) and optimization gates (actionable).
    """
    ps = _load_pipeline_settings()
    steps: list[dict] = []

    def _run_check(name: str, enabled_key: str, required_key: str, check_fn, *args):
        enabled = ps.get(enabled_key, True)
        required = ps.get(required_key, True)
        if not enabled:
            steps.append({"name": name, "status": "skipped", "detail": "Disabled in settings",
                          "actionable": None})
            return
        ok, detail, *extra = check_fn(*args)
        if ok:
            steps.append({"name": name, "status": "passed", "detail": detail,
                          "actionable": None, "extra": extra[0] if extra else None})
        elif required:
            steps.append({"name": name, "status": "failed", "detail": detail,
                          "actionable": _action_for_check(name), "extra": extra[0] if extra else None})
        else:
            steps.append({"name": name, "status": "warning", "detail": detail,
                          "actionable": _action_for_check(name), "extra": extra[0] if extra else None})

    # --- Paper trading metric checks (informational, not actionable) ---
    _run_check("paper_duration", "paper_live_gate_paper_duration_enabled",
               "paper_live_gate_paper_duration_required",
               _check_paper_duration, strategy_id)
    _run_check("paper_trades", "paper_live_gate_paper_trades_enabled",
               "paper_live_gate_paper_trades_required",
               _check_paper_trades, strategy_id)
    _run_check("paper_return", "paper_live_gate_paper_return_enabled",
               "paper_live_gate_paper_return_required",
               _check_paper_return, strategy_id)
    _run_check("paper_drawdown", "paper_live_gate_paper_drawdown_enabled",
               "paper_live_gate_paper_drawdown_required",
               _check_paper_drawdown, strategy_id)

    # --- Optimization gates (actionable) ---
    _run_check("optimization", "paper_live_gate_optimization_enabled",
               "paper_live_gate_optimization_required",
               _check_optimization_exists, strategy_id)
    _run_check("params_applied", "paper_live_gate_params_applied_enabled",
               "paper_live_gate_params_applied_required",
               _check_params_applied, strategy_id)
    _run_check("confirmation_backtest", "paper_live_gate_confirmation_backtest_enabled",
               "paper_live_gate_confirmation_backtest_required",
               _check_confirmation_backtest, strategy_id)

    ready = all(s["status"] in ("passed", "skipped", "warning") for s in steps)
    return {"ready": ready, "steps": steps, "strategy_id": strategy_id}


def _check_paper_duration(strategy_id: str) -> tuple[bool, str]:
    """Check if strategy has been in paper stage long enough."""
    config = load_pipeline_config()
    gate = config.get("paper_trading", {})
    min_days = int(gate.get("min_paper_days", 14))
    row = _load_strategy_row_for_gate(strategy_id)
    if not row:
        return False, "Strategy not found"
    stage_since = str(row["stage_changed_at"] or "").strip()
    if not stage_since:
        return False, "No stage timestamp found"
    try:
        started = datetime.fromisoformat(stage_since)
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - started).days
        if days >= min_days:
            return True, f"Paper duration: {days}/{min_days} days"
        return False, f"Insufficient paper duration: {days}/{min_days} days"
    except Exception:
        return False, "Could not parse stage timestamp"


def _check_paper_trades(strategy_id: str) -> tuple[bool, str]:
    """Check if strategy has enough closed paper trades."""
    config = load_pipeline_config()
    gate = config.get("paper_trading", {})
    min_trades = int(gate.get("min_closed_trades", 50))
    row = _load_strategy_row_for_gate(strategy_id)
    if not row:
        return False, "Strategy not found"
    stage_since = _paper_trade_window_since(row)
    params: list[object] = [strategy_id]
    where_since = ""
    if stage_since:
        where_since = " AND datetime(closed_at) >= datetime(?)"
        params.append(stage_since)
    with get_db() as conn:
        count_row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM trades "
            "WHERE COALESCE(strategy_id, strategy) = ? "
            "AND status = 'CLOSED' AND pnl_pct IS NOT NULL "
            "AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%'"
            + where_since,
            tuple(params),
        ).fetchone()
    total = int(count_row["cnt"] or 0) if count_row else 0
    if total >= min_trades:
        return True, f"Paper trades: {total}/{min_trades}"
    return False, f"Insufficient paper trades: {total}/{min_trades}"


def _check_paper_return(strategy_id: str) -> tuple[bool, str]:
    """Check if strategy has positive paper return."""
    row = _load_strategy_row_for_gate(strategy_id)
    if not row:
        return False, "Strategy not found"
    stage_since = _paper_trade_window_since(row)
    params: list[object] = [strategy_id]
    where_since = ""
    if stage_since:
        where_since = " AND datetime(closed_at) >= datetime(?)"
        params.append(stage_since)
    with get_db() as conn:
        trade_rows = conn.execute(
            "SELECT COALESCE(net_pnl_pct, pnl_pct) AS pnl_pct FROM trades "
            "WHERE COALESCE(strategy_id, strategy) = ? "
            "AND status = 'CLOSED' AND pnl_pct IS NOT NULL "
            "AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%'"
            + where_since,
            tuple(params),
        ).fetchall()
    pnls = [float(r["pnl_pct"]) for r in trade_rows if r["pnl_pct"] is not None]
    live = compute_live_metrics(pnls)
    total_return = float(live.get("total_return_pct", 0.0))
    if total_return > 0:
        return True, f"Paper return: {total_return:.2f}%"
    return False, f"Paper return not positive: {total_return:.2f}%"


def _check_paper_drawdown(strategy_id: str) -> tuple[bool, str]:
    """Check if strategy paper drawdown is within limits."""
    config = load_pipeline_config()
    gate = config.get("paper_trading", {})
    max_dd_limit = float(gate.get("max_drawdown_pct", 0.15))
    row = _load_strategy_row_for_gate(strategy_id)
    if not row:
        return False, "Strategy not found"
    stage_since = _paper_trade_window_since(row)
    params: list[object] = [strategy_id]
    where_since = ""
    if stage_since:
        where_since = " AND datetime(closed_at) >= datetime(?)"
        params.append(stage_since)
    with get_db() as conn:
        trade_rows = conn.execute(
            "SELECT COALESCE(net_pnl_pct, pnl_pct) AS pnl_pct FROM trades "
            "WHERE COALESCE(strategy_id, strategy) = ? "
            "AND status = 'CLOSED' AND pnl_pct IS NOT NULL "
            "AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%'"
            + where_since,
            tuple(params),
        ).fetchall()
    pnls = [float(r["pnl_pct"]) for r in trade_rows if r["pnl_pct"] is not None]
    live = compute_live_metrics(pnls)
    max_dd = float(live.get("max_drawdown_pct", 0.0))
    if max_dd < max_dd_limit:
        return True, f"Paper drawdown: {max_dd*100:.2f}% (limit {max_dd_limit*100:.2f}%)"
    return False, f"Paper drawdown too high: {max_dd*100:.2f}% (limit {max_dd_limit*100:.2f}%)"


def _load_strategy_row_for_gate(strategy_id: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT metrics, verdict, stage_changed_at, created_at FROM strategies WHERE id = ?",
            (strategy_id,),
        ).fetchone()


def _paper_trade_window_since(row) -> str:
    """Lower bound for the paper trade-evidence window.

    L-19 (2026-06-09 audit): prefer ``stage_changed_at``; fall back to
    ``created_at`` when it is missing so an empty stage timestamp can never
    unbound the evidence window to all history. Returns '' only when both are
    missing.
    """
    if not row:
        return ""
    stage_since = str(row["stage_changed_at"] or "").strip()
    if stage_since:
        return stage_since
    return str(row["created_at"] or "").strip()


def _load_gauntlet_artifact_counts(strategy_id: str) -> dict[str, int]:
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT LOWER(TRIM(COALESCE(result_type, 'backtest'))) AS result_type,
                   COUNT(*) AS total
            FROM backtest_results
            WHERE strategy_id = ?
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
              AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) IN ('optimization', 'walk_forward')
            GROUP BY LOWER(TRIM(COALESCE(result_type, 'backtest')))
            """,
            (strategy_id,),
        ).fetchall()

    counts = {"optimization": 0, "walk_forward": 0}
    for row in rows:
        result_type = str(row["result_type"] or "").strip().lower()
        if result_type in counts:
            counts[result_type] = int(row["total"] or 0)
    return counts


def _resolve_live_allocation_pct(
    days_in_stage: int,
    config: dict,
    strategy_id: str | None = None,
) -> float:
    """Resolve allocation percentage with P4-7 performance gates.

    Time-based ramp schedule, but requires performance checks to advance tiers.
    Freezes on half-retirement-threshold drawdown.
    Force-retires on hard drawdown breach.
    """
    live_cfg = config.get("live_graduated", {})
    schedule = live_cfg.get("allocation_schedule", [])
    if not isinstance(schedule, list):
        schedule = []
    weeks = max(1, int(days_in_stage // 7) + 1)

    time_based_allocation = 100.0
    for row in schedule:
        if not isinstance(row, dict):
            continue
        try:
            start = int(row.get("week_start", 1))
            end = int(row.get("week_end", 999))
            allocation = float(row.get("allocation_pct", 100))
        except Exception:
            continue
        if start <= weeks <= end:
            time_based_allocation = max(0.0, min(100.0, allocation))
            break

    # P4-7: Performance-gated ramp — check live performance before allowing increase
    if strategy_id:
        try:
            drift_data = kv_get("paper_live_drift")
            if isinstance(drift_data, dict):
                for r in drift_data.get("results", []):
                    if r.get("strategy_id") != strategy_id:
                        continue
                    if r.get("flagged"):
                        # Freeze allocation — do not ramp up
                        log.info(
                            "P4-7: Allocation freeze for %s — drift flagged: %s",
                            strategy_id, r.get("flag_reasons"),
                        )
                        return min(time_based_allocation, 25.0)  # Cap at 25%

            # Check hard drawdown threshold
            kill_switch_pct = float(live_cfg.get("decay_kill_switch_pct", 0.30))
            half_threshold = kill_switch_pct / 2.0

            baseline = kv_get(f"graduation_baseline:{strategy_id}")
            if isinstance(baseline, dict):
                baseline_sharpe = float(baseline.get("backtest_sharpe", 0) or 0)
                if baseline_sharpe > 0:
                    with get_db() as conn:
                        recent = conn.execute(
                            """SELECT pnl_pct FROM trades
                               WHERE COALESCE(strategy_id, strategy) = ?
                                 AND status = 'CLOSED' AND pnl_pct IS NOT NULL
                                 AND LOWER(COALESCE(execution_type, '')) LIKE 'live%'
                               ORDER BY closed_at DESC LIMIT 20""",
                            (strategy_id,),
                        ).fetchall()
                    pnls = [float(r["pnl_pct"]) for r in recent if r["pnl_pct"] is not None]
                    if len(pnls) >= 3:
                        cumulative = sum(pnls)
                        if cumulative < -(half_threshold * 100):
                            return min(time_based_allocation, 25.0)
        except Exception:
            pass

    return time_based_allocation


def _load_metrics_blob(row) -> dict:
    if not row:
        return {}
    try:
        parsed = json.loads(row["metrics"]) if isinstance(row["metrics"], str) else (row["metrics"] or {})
    except Exception:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _unwrap_metrics_dict(section: object) -> dict:
    """Return the inner ``metrics`` dict when a section nests one, else the section.

    Backtest payloads store IS/OOS either flat (``{"sharpe": ...}``) or nested
    (``{"metrics": {"sharpe": ...}}``). Shared by the quick-screen, gauntlet and
    paper gates so all three read the same shape (M-14 2026-06-09 audit).
    """
    if not isinstance(section, dict):
        return {}
    nested = section.get("metrics")
    if isinstance(nested, dict):
        return nested
    return section


def _metrics_section(metrics: dict, *keys: str) -> dict:
    """Return the first present nested section (unwrapped) for any of ``keys``."""
    if not isinstance(metrics, dict):
        return {}
    for key in keys:
        section = metrics.get(key)
        if isinstance(section, dict) and section:
            return _unwrap_metrics_dict(section)
    return {}


def _resolve_full_sample_trade_count(metrics: dict) -> float | None:
    """Best-available full-sample closed-trade count for the paper (capital) gate.

    Backtest blobs store the IS and OOS legs separately (disjoint train/test
    windows) with the top-level ``total_trades`` mirroring the OOS leg, so the
    full statistical sample is ``IS + OOS``. Fall back to whichever single signal
    exists for blobs without an IS/OOS split (or a flat ``trade_count``). Taking
    the max keeps the floor from false-rejecting a strategy that records its
    sample under only one of these shapes. Returns ``None`` only when no
    trade-count signal is present at all (a malformed/empty metrics blob).
    """
    is_n = _coerce_optional_float(_metrics_section(metrics, "in_sample").get("total_trades"))
    oos_n = _coerce_optional_float(_metrics_section(metrics, "out_of_sample").get("total_trades"))
    top_n = _coerce_optional_float(metrics.get("total_trades", metrics.get("trade_count")))
    candidates: list[float] = []
    if is_n is not None and oos_n is not None:
        candidates.append(is_n + oos_n)
    candidates.extend(v for v in (top_n, is_n, oos_n) if v is not None)
    return max(candidates) if candidates else None


def _extract_is_oos_sharpe(metrics: dict) -> tuple[float, float]:
    """Extract (IS Sharpe, OOS Sharpe) — nested ``in_sample``/``out_of_sample``
    sections first, flat ``is_sharpe``/``oos_sharpe`` keys as fallback.

    M-14 (2026-06-09 audit): the paper->live gate read only the flat keys, which
    real backtest metrics never carry (they nest under in_sample/out_of_sample),
    so the OOS>>IS overfitting check was dead code.
    """
    if not isinstance(metrics, dict):
        return 0.0, 0.0
    in_sample = _metrics_section(metrics, "in_sample", "is")
    out_of_sample = _metrics_section(metrics, "out_of_sample", "oos")

    def _sharpe_of(section: dict, flat_key: str) -> float:
        if section:
            return _coerce_float(section.get("sharpe", section.get("sharpe_ratio", 0.0)))
        return _coerce_float(metrics.get(flat_key, 0.0))

    return _sharpe_of(in_sample, "is_sharpe"), _sharpe_of(out_of_sample, "oos_sharpe")


def _canonicalize_gauntlet_verdict_test(name: object) -> str:
    normalized = str(name or "").strip().lower()
    return _GAUNTLET_VERDICT_ALIASES.get(normalized, normalized)


def _validation_row_to_verdict_payload(result_type: str, metrics: dict, config: dict) -> dict:
    verdict = str(metrics.get("verdict") or "").strip().upper()
    raw_status = str(metrics.get("status") or config.get("status") or "").strip().lower()
    if verdict == "PASS":
        status = "pass"
    elif verdict == "FAIL":
        status = "fail"
    elif raw_status in _GAUNTLET_SUCCESS_STATUSES:
        status = "pass"
    elif raw_status in _GAUNTLET_FAILURE_STATUSES:
        status = "fail"
    elif raw_status == "warn":
        status = "warn"
    else:
        status = "fail"

    if result_type == "walk_forward":
        splits = metrics.get("splits") if isinstance(metrics.get("splits"), list) else []
        # De-noise the fold pass rate: only judge folds that actually traded enough
        # to produce a meaningful OOS Sharpe. Counting near-empty folds in the
        # DENOMINATOR penalized a strategy for windows it legitimately didn't trade
        # (e.g. a trend system that sits out a flat fold), dragging pass_rate down
        # toward a false reject. wfa_min_fold_trades is wired (Settings > Lab).
        try:
            _min_fold_trades = int(
                (load_pipeline_config().get("robustness_thresholds") or {}).get("wfa_min_fold_trades", 5) or 5
            )
        except Exception:
            _min_fold_trades = 5
        passed_splits = 0
        evaluated_splits = 0
        raw_passed = 0  # sharpe>0 over ALL splits — legacy/fixture fallback
        for split in splits:
            if not isinstance(split, dict):
                continue
            oos = split.get("out_of_sample") if isinstance(split.get("out_of_sample"), dict) else {}
            sharpe = _coerce_float(oos.get("sharpe", oos.get("sharpe_ratio")), 0.0)
            if sharpe > 0:
                raw_passed += 1
            oos_trades = int(_coerce_float(oos.get("total_trades", oos.get("trades")), 0.0) or 0)
            if oos_trades < _min_fold_trades:
                continue  # too few trades to judge this fold either way
            evaluated_splits += 1
            if sharpe > 0:
                passed_splits += 1
        if evaluated_splits > 0:
            fold_count = evaluated_splits
            pass_rate = float(passed_splits / evaluated_splits)
        else:
            # No fold reported a per-fold trade count (legacy results / test
            # fixtures): fall back to the raw sharpe-based rate over all splits so
            # the de-noising never makes pass_rate worse than the old behavior.
            fold_count = len(splits)
            pass_rate = float(raw_passed / fold_count) if fold_count > 0 else 0.0
        # For walk_forward, 'passed' and 'status' MUST reflect actual fold pass rate,
        # not just the raw verdict string (which may fail for non-fold reasons like
        # negative avg IS Sharpe or IS->OOS degradation). If enough OOS folds are
        # positive, the WFA counts as passed for paper promotion.
        #
        # Rationale: the WFA verdict in robustness.py uses strict absolute thresholds
        # (avg_is > 0, degradation <= 0.35, avg_oos >= 0) designed for live money.
        # At the paper stage we only care about OOS consistency across folds — the IS
        # period that trained each fold is not "unseen" data, so IS Sharpe being
        # negative in some folds is expected and not a rejection signal by itself.
        # The paper->live gate (_strict_robustness_reject) enforces full strictness.
        #
        # Prior code had `False if status == "fail"` which short-circuited on ANY
        # WFA verdict failure (including non-fold reasons), contradicting the stated
        # intent. The ONLY paper-gate hard check on WFA is the fold consistency score.
        try:
            # _coerce_ratio_threshold at point-of-use as well as at the config boundary
            # (_RATIO_THRESHOLD_PATHS): accepts 60 and 0.60 identically, clamps to [0, 1].
            _fold_min = _coerce_ratio_threshold(
                (load_pipeline_config().get("robustness_thresholds") or {}).get("wfa_fold_pass_rate_min", 0.6),
                0.6,
            )
        except Exception:
            _fold_min = 0.6
        # Use fold pass rate as the sole paper-gate criterion, regardless of the
        # overall WFA verdict (which may fail for non-fold reasons like negative IS).
        wfa_passed = (pass_rate >= _fold_min if fold_count > 0 else status == "pass")
        wfa_status = "pass" if wfa_passed else "fail"
        aggregate_oos = metrics.get("aggregate_oos") if isinstance(metrics.get("aggregate_oos"), dict) else {}
        oos_trades = (
            metrics.get("total_oos_trades")
            or metrics.get("oos_trades")
            or aggregate_oos.get("total_trades")
        )
        payload = {
            "status": wfa_status,
            "passed": wfa_passed,
            "folds": fold_count,
            "pass_rate": pass_rate,
            "degradation": _coerce_optional_float(metrics.get("degradation") or metrics.get("sharpe_degradation")),
            "avg_oos_sharpe": _coerce_optional_float(
                metrics.get("avg_oos_sharpe")
                or aggregate_oos.get("sharpe")
                or aggregate_oos.get("sharpe_ratio")
            ),
            # verdict is overridden by fold pass-rate decision — if the fold
            # consistency check passes, report PASS here so _verdict_payload_failed
            # does not re-reject on the raw stored verdict (which may be FAIL for
            # non-fold reasons like negative avg IS Sharpe).
            "verdict": "PASS" if wfa_passed else verdict,
            "raw_verdict": verdict,  # preserve for auditability
        }
        if oos_trades is not None:
            payload["total_oos_trades"] = int(_coerce_float(oos_trades, 0.0) or 0)
        return payload

    if result_type == "monte_carlo":
        p95_ratio = _coerce_optional_float(metrics.get("max_dd_p95_ratio"))
        if p95_ratio is None:
            dd = metrics.get("drawdown_distribution") if isinstance(metrics.get("drawdown_distribution"), dict) else {}
            p95_ratio = _coerce_float(dd.get("p95"), 0.0) / 100.0
        prob_profitable = _coerce_optional_float(metrics.get("prob_profitable"))
        percentile_score = _coerce_optional_float(metrics.get("percentile_score") or metrics.get("robustness_pct"))
        if percentile_score is None and prob_profitable is not None:
            percentile_score = prob_profitable / 100.0
        return {
            "status": status,
            "passed": status == "pass",
            "max_dd_p95": float(p95_ratio or 0.0),
            "n_trades": int(_coerce_float(metrics.get("n_trades") or metrics.get("trade_count"), 0.0) or 0),
            "n_simulations": int(_coerce_float(metrics.get("n_simulations") or metrics.get("simulations"), 0.0) or 0),
            "percentile_rank": _coerce_optional_float(metrics.get("percentile_rank")),
            "percentile_score": percentile_score,
            "prob_profitable": prob_profitable,
            "verdict": verdict,
        }

    if result_type == "param_jitter":
        return {
            "status": status,
            "passed": status == "pass",
            "pct_positive_sharpe": _coerce_float(metrics.get("pct_positive_sharpe"), 0.0),
            # Project the analysis' pass_rate (0-1) so the param-jitter pass-rate floor
            # gate can actually read it; without this the calibration gate was dead.
            "pass_rate": _coerce_optional_float(metrics.get("pass_rate")),
            "verdict": verdict,
        }

    if result_type == "cost_stress":
        # BUGFIX: the cost-stress test stores the stressed Sharpe NESTED under
        # `stressed.sharpe` (robustness.py), not as a top-level `stressed_sharpe`.
        # The old extraction read the missing top-level key, so the gate's
        # cost-stress floor silently never fired (dead gate). Read the nested
        # value (keeping the legacy top-level path as a fallback).
        _stressed_block = metrics.get("stressed") if isinstance(metrics.get("stressed"), dict) else {}
        _stressed_sharpe = metrics.get("stressed_sharpe")
        if _stressed_sharpe is None:
            _stressed_sharpe = _stressed_block.get("sharpe")
        return {
            "status": status,
            "passed": status == "pass",
            "degradation_pct": _coerce_float(metrics.get("degradation_pct"), 0.0),
            "stressed_sharpe": _coerce_optional_float(_stressed_sharpe),
            "verdict": verdict,
        }

    if result_type == "regime_split":
        return {
            "status": status,
            "passed": status == "pass",
            "n_regimes": int(_coerce_float(metrics.get("n_regimes"), 0.0) or 0),
            "n_trades": int(_coerce_float(metrics.get("n_trades") or metrics.get("trade_count"), 0.0) or 0),
            # Project profitable-regime share (0-1) so the regime profitability floor
            # gate can read it; without this the calibration gate was dead.
            "profitable_regime_pct": _coerce_optional_float(metrics.get("profitable_regime_share")),
            "verdict": verdict,
        }

    return {"status": status, "passed": status == "pass", "verdict": verdict}


def _extract_gauntlet_verdict_payloads(strategy_id: str, row, metrics: dict) -> tuple[dict[str, object], str | None]:
    payloads: dict[str, object] = {}
    fallback_payloads: dict[str, object] = {}

    metrics_blob = metrics if isinstance(metrics, dict) else {}
    verdict_tests = metrics_blob.get("verdict_tests")
    if isinstance(verdict_tests, dict):
        for test_name, payload in verdict_tests.items():
            normalized_name = _canonicalize_gauntlet_verdict_test(test_name)
            if normalized_name and isinstance(payload, dict):
                fallback_payloads.setdefault(normalized_name, payload)

    verdict_blob = _parse_json_blob(row["verdict"], {}) if row and row["verdict"] else {}
    stored_tests = verdict_blob.get("tests") if isinstance(verdict_blob, dict) else {}
    if isinstance(stored_tests, dict):
        for test_name, payload in stored_tests.items():
            normalized_name = _canonicalize_gauntlet_verdict_test(test_name)
            if normalized_name and isinstance(payload, dict):
                fallback_payloads.setdefault(normalized_name, payload)

    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT result_type, metrics_json, config_json
            FROM backtest_results
            WHERE strategy_id = ?
              AND (deleted_at IS NULL OR TRIM(COALESCE(deleted_at, '')) = '')
              AND LOWER(TRIM(COALESCE(result_type, 'backtest'))) IN (
                  'walk_forward', 'monte_carlo', 'param_jitter', 'cost_stress', 'regime_split'
              )
            ORDER BY datetime(created_at) DESC, result_id DESC
            """,
            (strategy_id,),
        ).fetchall()

    for result_row in rows:
        normalized_type = _canonicalize_gauntlet_verdict_test(result_row["result_type"])
        if normalized_type in payloads:
            continue
        metrics_blob = _parse_json_blob(result_row["metrics_json"], {})
        config_blob = _parse_json_blob(result_row["config_json"], {})
        if not isinstance(metrics_blob, dict):
            metrics_blob = {}
        if not isinstance(config_blob, dict):
            config_blob = {}
        status = str(metrics_blob.get("status") or config_blob.get("status") or "").strip().lower()
        if status in {"pending", "queued", "running", "started", "submitted"}:
            continue
        payload = _validation_row_to_verdict_payload(normalized_type, metrics_blob, config_blob)
        legitimacy_payload = dict(config_blob)
        legitimacy_payload.update(metrics_blob)
        legitimacy_payload.update(payload)
        if normalized_type == "monte_carlo" and "min_trades" not in legitimacy_payload:
            legitimacy_payload["min_trades"] = 10
        legitimacy = validate_robustness_payload(normalized_type, legitimacy_payload)
        if not legitimacy.get("ok"):
            payload["status"] = "fail"
            payload["passed"] = False
            payload["error"] = str(legitimacy.get("reason") or "validation payload is not legitimate")
        payloads[normalized_type] = payload

    for test_name, payload in fallback_payloads.items():
        existing = payloads.get(test_name)
        if not isinstance(existing, dict) or not isinstance(payload, dict):
            payloads.setdefault(test_name, payload)
            continue
        merged = dict(existing)
        for field_name, field_value in payload.items():
            if field_name in {"status", "passed"}:
                continue
            current_value = merged.get(field_name)
            if current_value in {None, "", 0, 0.0} and field_value not in {None, ""}:
                merged[field_name] = field_value
        payloads[test_name] = merged

    overall_status: str | None = None
    if payloads:
        overall_status = "pass"
        for value in payloads.values():
            if isinstance(value, dict) and _verdict_payload_failed(value):
                overall_status = "fail"
                break
    return payloads, overall_status


def _verdict_payload_failed(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False

    if payload.get("error"):
        return True

    status = str(payload.get("status") or "").strip().lower()
    if status in _GAUNTLET_FAILURE_STATUSES:
        return True

    verdict = str(payload.get("verdict") or "").strip().lower()
    if verdict in _GAUNTLET_FAILURE_STATUSES:
        return True

    ok = payload.get("ok")
    if isinstance(ok, bool):
        return not ok

    passed = payload.get("passed")
    if isinstance(passed, bool):
        return not passed

    return False



def verify_backtest_persisted(strategy_id: str) -> tuple[bool, str, dict]:
    """Verify that backtest results exist in SQLite or ChromaDB before allowing lifecycle transitions."""
    from forven.vectordb import search_backtest_results
    from forven.db import get_db
    
    # First check SQLite
    with get_db() as conn:
        sqla_result = conn.execute(
            "SELECT result_id, created_at, metrics_json FROM backtest_results WHERE strategy_id = ? ORDER BY created_at DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()

    if sqla_result and sqla_result["metrics_json"]:
        return True, "Found SQLite backtest results", {"source": "sqlite", "result_id": sqla_result["result_id"]}
    
    # Also check ChromaDB
    # P1-7: search_backtest_results returns list[dict], not raw chroma result.
    try:
        chroma_results = search_backtest_results(
            query=f"strategy_id:{strategy_id}",
            n_results=1,
        )
        if chroma_results and len(chroma_results) > 0:
            return True, "Found ChromaDB backtest results", {"source": "chromadb"}
    except Exception as exc:
        log.warning("ChromaDB backtest verification failed for %s: %s", strategy_id, exc)
    
    return False, "No backtest results found in SQLite or ChromaDB", {}


def verify_backtest_exists_for_stage_transition(strategy_id: str, target_stage: str) -> tuple[bool, str]:
    """Verify backtest data exists before allowing stage transitions. Prevents phantom container failures.

    Only enforced for paper and live_graduated stages — earlier stages (quick_screen, gauntlet)
    don't require backtest results because strategies are still being evaluated.
    Accepts strategy-level metrics as valid evidence of backtesting (set by
    _sync_strategy_metrics_and_promote_if_eligible), not just the backtest_results table.
    """
    stages_requiring_backtest = {"paper", "live_graduated"}

    if target_stage.lower() not in stages_requiring_backtest:
        return True, "Stage transition allowed without backtest verification"

    # Check if the strategy already has backtest metrics stored on the record itself
    # (set by _sync_strategy_metrics_and_promote_if_eligible or run_testing_step)
    import json as _json
    from forven.db import get_db as _get_db
    with _get_db() as conn:
        strat_row = conn.execute(
            "SELECT metrics FROM strategies WHERE id = ?", (strategy_id,),
        ).fetchone()
    if strat_row:
        try:
            metrics = _json.loads(strat_row["metrics"] or "{}")
            if metrics.get("sharpe") is not None or metrics.get("total_trades") is not None:
                return True, "Verified: strategy has backtest metrics"
        except (_json.JSONDecodeError, KeyError):
            pass

    # Fall back to checking the backtest_results table and ChromaDB
    has_data, message, _ = verify_backtest_persisted(strategy_id)

    if not has_data:
        return False, f"BLOCKED: Cannot transition to {target_stage} - no backtest results persisted. {message}"

    return True, f"Verified: {message}"

def _evaluate_source_divergence_gate(strategy_id: str, general_settings: Any) -> tuple[bool, str]:
    """Cache-only source-divergence promotion gate.

    Refuses to admit a strategy into a capital-bearing stage when the price series
    it was VALIDATED on (e.g. Binance parquet) diverges materially from the venue it
    will TRADE on (HyperLiquid). The divergence is PRE-COMPUTED out of band by the
    ``forven-source-reconciliation`` job and read here cache-only via ``kv_get``
    (SELECT-only). This gate MUST NOT fetch or write: ``transition_stage`` runs
    ``evaluate_promotion`` while deliberately holding its connection READ-ONLY (the
    first write is deferred until every gate passes — see ``brain.py:1181-1188``), so
    a blocking write reachable from here would self-deadlock against that deferred
    writer for the full busy-timeout. Cache-only reads preserve that invariant.

    Fail-open by design: a missing / stale / insufficient-overlap reading allows the
    promotion (unless the operator sets ``block_when_missing``) so a never-reconciled
    strategy never jams the funnel. Ships inert (``enabled=False``).
    """
    cfg: dict[str, Any] = {}
    if isinstance(general_settings, dict):
        engine = general_settings.get("data_engine_settings")
        if isinstance(engine, dict):
            candidate = engine.get("source_reconciliation")
            if isinstance(candidate, dict):
                cfg = candidate
    if not cfg.get("enabled"):
        return True, "source reconciliation disabled"

    block_when_missing = bool(cfg.get("block_when_missing", False))

    def _coerce(value: Any, fallback: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    max_threshold = _coerce(cfg.get("max_divergence_pct"), 2.0)
    staleness_hours = _coerce(cfg.get("staleness_hours"), 24.0)

    # Current symbol/timeframe (SELECT-only; safe inside the outer write txn).
    with get_db() as conn:
        row = conn.execute(
            "SELECT symbol, timeframe FROM strategies WHERE id = ?", (strategy_id,)
        ).fetchone()
    symbol = str((row["symbol"] if row else "") or "").strip().upper()
    timeframe = str((row["timeframe"] if row else "") or "").strip().lower() or "1h"
    if not symbol or symbol == "GENERIC":
        return True, "no symbol to reconcile"

    payload = kv_get(f"forven:data:divergence:{symbol}:{timeframe}")

    def _missing(reason: str) -> tuple[bool, str]:
        if block_when_missing:
            return False, (
                f"Source reconciliation pending ({reason}) — divergence not yet "
                f"computed for {symbol} {timeframe}"
            )
        return True, f"source reconciliation unavailable ({reason}) — allowing"

    if not isinstance(payload, dict):
        return _missing("no data")
    if str(payload.get("status") or "") != "ok":
        return _missing(str(payload.get("status") or "not ok"))

    checked_at = payload.get("checked_at")
    if checked_at:
        try:
            stamped = datetime.fromisoformat(str(checked_at).replace("Z", "+00:00"))
            if stamped.tzinfo is None:
                stamped = stamped.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - stamped).total_seconds() / 3600.0
            if age_hours > staleness_hours:
                return _missing(f"stale {age_hours:.0f}h")
        except (TypeError, ValueError):
            pass

    max_div = _coerce(payload.get("max_divergence_pct"), -1.0)
    if max_div < 0:
        return _missing("unparseable divergence")
    if max_div > max_threshold:
        backtest_source = payload.get("backtest_source", "backtest")
        live_venue = payload.get("live_venue", "live")
        overlap = payload.get("overlap_bars", 0)
        mean_div = _coerce(payload.get("mean_divergence_pct"), 0.0)
        log.info(
            "source-divergence gate BLOCK %s: max %.2f%% (mean %.2f%%) > %.2f%% (%s vs %s)",
            strategy_id, max_div, mean_div, max_threshold, backtest_source, live_venue,
        )
        return False, (
            f"Source price divergence max {max_div:.2f}% (mean {mean_div:.2f}%) exceeds "
            f"{max_threshold:.2f}% ({backtest_source} vs {live_venue}, {overlap} bars) — "
            f"re-validate on the trade venue's data before promotion"
        )
    return True, f"source divergence {max_div:.2f}% within {max_threshold:.2f}%"


def _paper_slot_competition_enabled(settings: object = None) -> bool:
    """Whether capital slots are limited to ONE strategy per symbol/timeframe.

    When True, the quality-aware duplicate tournament (gauntlet entry), the paper
    slot-guard, the capital-slot dedupe sweep, and the paper WIP cap all apply, so
    a market holds a single champion and challengers must dethrone it.

    When False (DEFAULT), every strategy that passes the gauntlet robustness gate is
    promoted to paper — no per-slot competition, no dethrone tournament, and no
    cap on how many strategies may paper-trade. The gauntlet quality gate itself is
    unchanged. Wired setting: forven:settings.paper_slot_competition_enabled.
    """
    if settings is None:
        settings = kv_get("forven:settings")
    return bool(settings.get("paper_slot_competition_enabled", False)) if isinstance(settings, dict) else False


def evaluate_promotion(strategy_id: str, from_stage: str, to_stage: str) -> tuple[bool, str]:
    """Single source of truth for all promotion decisions in the 4-step gauntlet."""
    config = load_pipeline_config()
    normalized_from = _normalize_pipeline_stage(from_stage)
    normalized_to = _normalize_pipeline_stage(to_stage)

    # Gate-bypass switches accelerate quick_screen/gauntlet iteration, but they must
    # NEVER skip the capital-bearing gates (paper, live_graduated) — otherwise an
    # unvetted strategy could enter a trading slot with zero robustness/evidence
    # checks. normalize_stage canonicalises all paper/live aliases, so the set is
    # alias-robust.
    _BYPASS_EXCLUDED_STAGES = {"archived", "rejected", "paper", "live_graduated"}
    if config.get("testing_mode") and normalized_to not in _BYPASS_EXCLUDED_STAGES:
        return True, "Passed via relaxed testing mode (gates bypassed)"

    # Also check paper_test_bypass_gates from general settings
    general_settings = kv_get("forven:settings")
    if isinstance(general_settings, dict) and general_settings.get("paper_test_bypass_gates_enabled"):
        if normalized_to not in _BYPASS_EXCLUDED_STAGES:
            return True, "Passed via paper test bypass gates setting"

    # --- Funding-data completeness gate (stage-aware) ---
    # A backtest that couldn't fully account for perp funding (missing funding
    # history) has slightly distorted PnL. That matters for LIVE capital, so we
    # BLOCK ->live_graduated until the data is backfilled and the strategy
    # re-tests. For PAPER we ALLOW it through: paper runs on testnet and measures
    # real funding directly, and "achievable paper / strict live" is the chosen
    # posture — holding a strategy that passed every robustness test out of paper
    # over a data-collection gap silently deletes winners. The scheduled funding
    # reconcile (now covering every asset strategies trade) backfills the asset so
    # a later ->live re-test has complete data. funding_applied is only set by
    # post-change backtests, so older strategies (no key) are unaffected.
    # NB: this runs inside the gate's open write txn — no network backfill here.
    if normalized_to == "live_graduated" and (
        not isinstance(general_settings, dict)
        or general_settings.get("backtest_include_funding", True)
    ):
        funding_metrics = _load_metrics_blob(_load_strategy_row_for_gate(strategy_id))
        if funding_metrics.get("funding_applied") and not funding_metrics.get("funding_complete", True):
            return False, (
                "Funding data incomplete — backfill funding history and re-run "
                "the backtest before live promotion"
            )

    # --- Symbol gate: require a valid trading symbol for forward stages ---
    if normalized_to not in {"archived", "rejected"}:
        with get_db() as conn:
            sym_row = conn.execute(
                "SELECT symbol FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
        current_sym = str((sym_row["symbol"] if sym_row else "") or "").strip().upper()
        if not current_sym or current_sym == "GENERIC":
            # Attempt auto-resolution from backtest results
            from forven.db import auto_assign_best_symbol
            assigned = auto_assign_best_symbol(strategy_id)
            if not assigned:
                return False, "No valid symbol — run backtests on at least one trading pair"

    # --- Source-reconciliation gate (cache-only) ---
    # Before a strategy bears capital, ensure the price series it was validated on
    # has not diverged from the venue it will trade on. The metric is pre-computed
    # out of band; this read is SELECT-only (kv_get) so it is safe inside the open
    # write txn. Logged under the gate of the FROM stage (gauntlet for ->paper,
    # paper for ->live_graduated) to match the other gates' rejection records.
    if normalized_to in {"paper", "live_graduated"}:
        div_ok, div_reason = _evaluate_source_divergence_gate(strategy_id, general_settings)
        if not div_ok:
            stage_label = "gauntlet" if normalized_to == "paper" else "paper"
            _log_gate_rejection_record(strategy_id, stage_label, div_reason, config)
            return False, div_reason

    # Guardrail 6: Active strategy correlation check (S00291)
    # Check for duplication with live/paper strategies before gauntlet promotion
    if normalized_to == "gauntlet":
        with get_db() as conn:
            live_rows = conn.execute(
                """SELECT s.id, s.name, s.symbol, s.timeframe, s.stage, s.metrics
                   FROM strategies s
                   WHERE s.stage IN ('paper', 'paper_trading', 'live_graduated', 'deployed')
                      AND s.symbol IS NOT NULL
                      AND s.symbol != ''
                      AND s.symbol != 'GENERIC'"""
            ).fetchall()

            new_row = conn.execute(
                "SELECT symbol, timeframe, metrics FROM strategies WHERE id = ?",
                (strategy_id,)
            ).fetchone()

        if new_row and _paper_slot_competition_enabled(general_settings):
            new_symbol = str(new_row["symbol"] or "").strip().upper()
            new_timeframe = str(new_row["timeframe"] or "").strip().lower()
            challenger_metrics = _load_metrics_blob(new_row)

            # Collect incumbents holding the same symbol/timeframe slot.
            colliding = [
                live
                for live in live_rows
                if str(live["symbol"] or "").strip().upper() == new_symbol
                and str(live["timeframe"] or "").strip().lower() == new_timeframe
            ]
            # Quality-aware tournament: block unless the challenger materially beats
            # every incumbent on the slot. A weaker/equal challenger is still rejected
            # (roster hygiene). A clearly better one passes AND queues a dethrone so
            # the occupied slot can free up rather than blocking the better strategy.
            beaten: list[tuple] = []
            sharpe_ceiling = _challenger_sharpe_ceiling(config)
            for live in colliding:
                incumbent_metrics = _load_metrics_blob(live)
                if _challenger_materially_beats(
                    challenger_metrics, incumbent_metrics, sharpe_ceiling=sharpe_ceiling
                ):
                    beaten.append((live, incumbent_metrics))
                else:
                    return False, f"Duplicate with active strategy {live['id']} (same {new_symbol} {new_timeframe})"
            for live, incumbent_metrics in beaten:
                with get_db() as dconn:
                    approval_id = _queue_challenger_dethrone(
                        conn=dconn,
                        incumbent_id=str(live["id"]),
                        incumbent_stage=str(live["stage"] or "paper").strip().lower(),
                        challenger_id=strategy_id,
                        challenger_sharpe=_metric_sharpe(challenger_metrics),
                        incumbent_sharpe=_metric_sharpe(incumbent_metrics),
                    )
                _maybe_auto_apply_dethrone(approval_id, general_settings)

        result = _evaluate_quick_screen_gate(strategy_id, config)
        if not result[0]:
            _log_gate_rejection_record(strategy_id, "quick_screen", result[1], config)
        return result
    if normalized_to == "paper":
        # Slot-guard: when slot-competition is enabled, keep one strategy per
        # symbol/timeframe in capital-bearing stages (a challenger waits for the
        # incumbent's slot to free via dethrone). When disabled (default), any
        # strategy that clears the gauntlet gate below is promoted regardless of how
        # many already trade the same market.
        if _paper_slot_competition_enabled(general_settings):
            with get_db() as conn:
                occ_rows = conn.execute(
                    """SELECT s.id, s.symbol, s.timeframe
                       FROM strategies s
                       WHERE s.stage IN ('paper', 'paper_trading', 'live_graduated', 'deployed')
                         AND s.id != ?
                         AND s.symbol IS NOT NULL
                         AND s.symbol != ''
                         AND s.symbol != 'GENERIC'""",
                    (strategy_id,),
                ).fetchall()
                self_row = conn.execute(
                    "SELECT symbol, timeframe FROM strategies WHERE id = ?", (strategy_id,)
                ).fetchone()
            if self_row:
                my_symbol = str(self_row["symbol"] or "").strip().upper()
                my_timeframe = str(self_row["timeframe"] or "").strip().lower()
                for occ in occ_rows:
                    occ_symbol = str(occ["symbol"] or "").strip().upper()
                    occ_timeframe = str(occ["timeframe"] or "").strip().lower()
                    if occ_symbol == my_symbol and occ_timeframe == my_timeframe:
                        return False, (
                            f"Slot occupied by incumbent {occ['id']} "
                            f"(same {my_symbol} {my_timeframe}) — awaiting dethrone"
                        )
        result = _evaluate_gauntlet_gate(strategy_id, config)
        if not result[0]:
            _log_gate_rejection_record(strategy_id, "gauntlet", result[1], config)
        return result
    if normalized_to == "live_graduated":
        result = _evaluate_paper_gate(strategy_id, config)
        if not result[0]:
            _log_gate_rejection_record(strategy_id, "paper", result[1], config)
        return result
    if normalized_to in {"archived", "rejected"}:
        return True, "Manual or policy-driven archival"
    return True, f"Transition from {normalized_from or from_stage} to {normalized_to or to_stage} allowed"


def _extract_reason_code(reason_text: str) -> str:
    """Extract a machine-readable reason code from gate rejection text."""
    text = reason_text.lower()
    # Error / no-evidence outcomes must NOT share the generic ``gate_reject``
    # bucket with genuine performance rejections. "No metrics available" means
    # the backtest never ran (db-lock / timeout / process-restart / blocked
    # import / codegen crash) — not evidence of a bad edge — and "zero trades"
    # is a config/no-signal outcome. Keeping them in their own buckets stops
    # them from feeding the repeated-failure counter that auto-archives losers.
    if "metrics available" in text:
        return "no_metrics_error"
    # Evidence-ABSENCE outcomes one stage later than no_metrics_error: the gauntlet
    # artifacts/tests have not been run or persisted YET (work queued, optimization in
    # flight, validation awaiting a re-run). These previously fell into wfa_reject via
    # the "walk"+"forward" match below and accumulated toward the repeated-failure
    # auto-archive even though they say nothing about edge quality. Classify them
    # before the quality-failure matches so they get their own (counter-exempt) codes.
    if "persisted optimization or walk-forward run" in text:
        return "artifacts_pending"
    if "stale validation tests" in text or "ordering violation" in text:
        return "stale_validation"
    if "zero trades" in text or "produces no signals" in text:
        return "zero_trade"
    # L-21 (2026-06-09 audit): paper warm-up rejections ("Insufficient paper
    # duration/sample/trades") are absence of forward evidence, not evidence of
    # a bad edge. A dedicated code replaces the brittle startswith/SQL-NOT-LIKE
    # text matching that previously carved these out of the dethrone counter.
    if text.startswith("insufficient paper"):
        return "insufficient_paper_evidence"
    if "divergence" in text:
        return "source_divergence_reject"
    if "overfit" in text:
        return "overfit_reject"
    if "s00552" in text:
        return "s00552_reject"
    if "s00152" in text:
        return "s00152_reject"
    if "sharpe" in text and ("low" in text or "gap" in text):
        return "sharpe_reject"
    if "drawdown" in text:
        return "drawdown_reject"
    if "return" in text and "low" in text:
        return "return_reject"
    if "win rate" in text:
        return "win_rate_reject"
    if "profit factor" in text or "pf" in text:
        return "profit_factor_reject"
    if "robustness" in text:
        return "robustness_reject"
    if "walk" in text and "forward" in text:
        return "wfa_reject"
    if "monte carlo" in text:
        return "monte_carlo_reject"
    if "missing" in text:
        return "missing_evidence"
    if "duplicate" in text:
        return "duplicate_reject"
    if "timeout" in text:
        return "timeout_reject"
    if "not found" in text:
        return "not_found"
    return "gate_reject"


def _load_metrics_snapshot_for_rejection(strategy_id: str) -> dict | None:
    """Load a compact metrics snapshot for rejection logging."""
    try:
        row = _load_strategy_row_for_gate(strategy_id)
        if not row:
            return None
        metrics = _load_metrics_blob(row)
        if not metrics:
            return None
        # Return compact subset
        return {
            k: metrics.get(k)
            for k in (
                "sharpe", "total_return_pct", "max_drawdown_pct", "win_rate",
                "profit_factor", "total_trades", "robustness_score",
                "composite_robustness_score",
            )
            if metrics.get(k) is not None
        }
    except Exception:
        return None


def _log_gate_rejection_record(strategy_id: str, gate: str, reason_text: str, config: dict):
    """P0-2/P3-1: Log structured gate rejection with failure taxonomy fields."""
    reason_code = _extract_reason_code(reason_text)
    metrics_snapshot = _load_metrics_snapshot_for_rejection(strategy_id)
    gate_config = config.get(gate, {})
    resolved_thresholds = {k: v for k, v in gate_config.items() if isinstance(v, (int, float, str, bool))}

    # P3-1: Enrich with strategy_type and regime_context for taxonomy
    strategy_type = None
    regime_context = None
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT name, symbol FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
            if row:
                name = str(row["name"] or "").strip().lower()
                # Infer strategy type from name
                for st in ("rsi_momentum", "ema_cross", "keltner", "bollinger", "macd",
                           "funding", "williams_r", "stochastic", "supertrend", "vwap",
                           "ichimoku", "adx_trend", "aroon", "hma_cross", "parabolic_sar"):
                    if st in name.replace("-", "_"):
                        strategy_type = st
                        break

                # Get current regime for the asset
                symbol = str(row["symbol"] or "").strip().upper()
                if symbol:
                    try:
                        # Cache-only: gate rejection is a hot path and runs inside
                        # transition_stage's open write transaction. A live fetch or
                        # cache-write here would stall on SQLite write contention.
                        from forven.regime import peek_cached_regime
                        state = peek_cached_regime(symbol)
                        regime_context = state.regime if state else None
                    except Exception:
                        pass
    except Exception:
        pass

    log_gate_rejection(
        strategy_id=strategy_id,
        gate=gate,
        reason_code=reason_code,
        reason_text=reason_text,
        metrics_snapshot=metrics_snapshot,
        resolved_thresholds=resolved_thresholds,
        strategy_type=strategy_type,
        regime_context=regime_context,
    )
    log.info("Gate rejection [%s/%s]: %s — %s", gate, reason_code, strategy_id, reason_text)

    # Auto-archive/recommend strategies that fail the same gate repeatedly
    _check_repeated_failure_auto_archive(strategy_id, gate, reason_code, reason_text)


_REPEATED_FAILURE_THRESHOLD = 5
# Reason codes that signal ABSENCE of evidence (tests not yet run/persisted, work in
# flight) rather than evidence of a bad edge. Exempt from the repeated-failure
# auto-archive counter — see _check_repeated_failure_auto_archive.
_EVIDENCE_ABSENCE_REASON_CODES = {
    "no_metrics_error",
    "artifacts_pending",
    "stale_validation",
    "missing_evidence",
    # Paper warm-up: not enough forward days/trades accumulated yet (L-21).
    "insufficient_paper_evidence",
}
_DETHRONE_APPROVAL_TYPE = "strategy_dethrone_recommendation"
_DETHRONE_MANUAL_STAGES = {"paper", "paper_trading", "live_graduated", "deployed"}
_DETHRONE_REVIEW_COOLDOWN_HOURS = 24


def _resolve_dethrone_target_stage(current_stage: str) -> str:
    normalized = normalize_stage(current_stage)
    if normalized in {"paper", "paper_trading"}:
        return "gauntlet"
    if normalized in {"live_graduated", "deployed"}:
        return "paper"
    return "archived"


def _dethrone_cooldown_key(strategy_id: str) -> str:
    return f"forven:dethrone:cooldown:{strategy_id}"


def _is_dethrone_cooldown_active(strategy_id: str) -> bool:
    raw = kv_get(_dethrone_cooldown_key(strategy_id))
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        ts = datetime.fromisoformat(raw.strip())
    except Exception:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) < (ts + timedelta(hours=_DETHRONE_REVIEW_COOLDOWN_HOURS))


def _queue_dethrone_recommendation(
    *,
    conn,
    strategy_id: str,
    current_stage: str,
    gate: str,
    reason_code: str,
    reason_text: str,
    failure_count: int,
) -> int | None:
    pending = conn.execute(
        """
        SELECT id FROM approvals
        WHERE approval_type = ?
          AND target_type = 'strategy'
          AND target_id = ?
          AND status = 'pending_approval'
        ORDER BY id DESC
        LIMIT 1
        """,
        (_DETHRONE_APPROVAL_TYPE, strategy_id),
    ).fetchone()
    if pending:
        return int(pending["id"])

    recommended_target_stage = _resolve_dethrone_target_stage(current_stage)
    approval_id = create_approval(
        approval_type=_DETHRONE_APPROVAL_TYPE,
        target_type="strategy",
        target_id=strategy_id,
        requested_status=recommended_target_stage,
        status="pending_approval",
        actor="policy",
        reason=(
            f"Dethrone recommendation: strategy hit repeated failures at {gate}/{reason_code} "
            f"({failure_count}x)"
        ),
        payload={
            "strategy_id": strategy_id,
            "current_stage": current_stage,
            "gate": gate,
            "reason_code": reason_code,
            "reason_text": reason_text,
            "failure_count": failure_count,
            "threshold": _REPEATED_FAILURE_THRESHOLD,
            "recommended_action": "dethrone",
            "recommended_target_stage": recommended_target_stage,
            "operator_required": True,
        },
        owner="ceo",
        conn=conn,
    )
    conn.execute(
        """
        INSERT INTO strategy_events
            (strategy_id, from_state, to_state, actor, reason, created_at)
        VALUES
            (?, ?, ?, 'policy', ?, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        """,
        (
            strategy_id,
            current_stage,
            current_stage,
            f"Dethrone recommendation queued in approvals (approval #{approval_id}) after repeated {gate}/{reason_code} failures",
        ),
    )
    return approval_id


def _clear_pending_dethrone_recommendations_for_strategy(conn, strategy_id: str, *, reason: str) -> int:
    rows = conn.execute(
        """
        SELECT id, payload FROM approvals
        WHERE approval_type = ?
          AND target_type = 'strategy'
          AND target_id = ?
          AND status = 'pending_approval'
        ORDER BY id DESC
        """,
        (_DETHRONE_APPROVAL_TYPE, strategy_id),
    ).fetchall()
    if not rows:
        return 0

    approval_ids: list[int] = []
    for row in rows:
        try:
            payload = json.loads(row["payload"]) if row["payload"] else {}
        except Exception:
            payload = {}
        # Challenger-driven dethrones mark a superior strategy waiting for the slot;
        # the incumbent's own insufficient-evidence clear must NOT wipe them.
        if isinstance(payload, dict) and payload.get("trigger") == "superior_challenger":
            continue
        approval_ids.append(int(row["id"]))

    if not approval_ids:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        UPDATE approvals
        SET status = 'denied',
            actor = 'policy',
            decision = 'auto_cleared',
            reason = ?,
            updated_at = ?,
            decided_at = ?
        WHERE id = ?
        """,
        [(reason, now, now, approval_id) for approval_id in approval_ids],
    )
    return len(approval_ids)


def _metric_sharpe(metrics: dict) -> float | None:
    """Read a strategy's Sharpe from a metrics blob, preferring 'sharpe'."""
    if not isinstance(metrics, dict):
        return None
    raw = metrics.get("sharpe")
    if raw is None:
        raw = metrics.get("sharpe_ratio")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# Above this Sharpe, an incumbent's extra Sharpe is treated as overfitting
# noise rather than a real edge a challenger must beat. Without a cap, an
# overfit incumbent (e.g. Sharpe 8-10 from a curve-fit on one regime) holds its
# symbol/timeframe slot forever because nothing can clear it by the 15% margin —
# the documented 2026-05-06 paper-promotion deadlock. Configurable via the
# pipeline `gauntlet.challenger_sharpe_comparison_ceiling` setting.
_DEFAULT_CHALLENGER_SHARPE_CEILING = 3.0


def _challenger_sharpe_ceiling(config: dict | None) -> float:
    """Resolve the incumbent Sharpe comparison ceiling from pipeline config."""
    try:
        gauntlet = (config or {}).get("gauntlet") if isinstance(config, dict) else None
        raw = (gauntlet or {}).get("challenger_sharpe_comparison_ceiling")
        ceiling = float(raw) if raw is not None else _DEFAULT_CHALLENGER_SHARPE_CEILING
    except (TypeError, ValueError):
        ceiling = _DEFAULT_CHALLENGER_SHARPE_CEILING
    return ceiling if ceiling > 0 else _DEFAULT_CHALLENGER_SHARPE_CEILING


def _challenger_materially_beats(
    challenger_metrics: dict,
    incumbent_metrics: dict,
    margin: float = 0.15,
    *,
    sharpe_ceiling: float | None = None,
) -> bool:
    """True when the challenger's Sharpe clears the incumbent's by `margin`.

    The incumbent's comparison Sharpe is capped at ``sharpe_ceiling`` (when set)
    so an overfit incumbent with an implausible Sharpe cannot defend its slot
    against a genuinely good challenger forever.

    Conservative by design: if either side lacks a usable Sharpe, return False so
    the duplicate gate keeps blocking (roster hygiene is preserved on missing data).
    """
    ch_s = _metric_sharpe(challenger_metrics)
    inc_s = _metric_sharpe(incumbent_metrics)
    if ch_s is None or inc_s is None:
        return False
    base = max(inc_s, 0.0)
    if sharpe_ceiling is not None and sharpe_ceiling > 0:
        base = min(base, float(sharpe_ceiling))
    threshold = base * (1.0 + margin)
    return ch_s >= threshold and ch_s > 0


def dedupe_capital_slots(*, actor: str = "pipeline_sweep", dry_run: bool = False) -> dict:
    """Archive redundant duplicates so each symbol/timeframe capital slot holds
    only the single best-Sharpe strategy.

    The slot/duplicate gate prevents NEW duplicates, but strategies promoted
    before that gate existed can pile multiple incumbents onto one slot, which
    forces a challenger to beat ALL of them and effectively freezes the slot
    (the documented 2026-05-06 ETH/USDT-1h deadlock). Keeps the highest-Sharpe
    incumbent per slot and archives the rest through the proper lifecycle path
    (force-archive + strategy_events). Idempotent: a slot with one occupant is a
    no-op, so it is safe to re-run.

    No-op when slot-competition is disabled (the default) — that mode deliberately
    allows multiple strategies per market, so de-duping them would fight the policy.
    """
    import logging as _logging

    from forven.brain import transition_stage

    if not _paper_slot_competition_enabled():
        return {"archived": [], "dry_run": dry_run, "slots_examined": 0,
                "skipped": "paper_slot_competition_disabled"}

    slots: dict[tuple[str, str], list[tuple[str, float]]] = {}
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, symbol, timeframe, stage, metrics FROM strategies
               WHERE stage IN ('paper', 'paper_trading', 'live_graduated', 'deployed')
                 AND symbol IS NOT NULL AND symbol != '' AND symbol != 'GENERIC'"""
        ).fetchall()
    for row in rows:
        sharpe = _metric_sharpe(_load_metrics_blob(row))
        key = (str(row["symbol"]).strip().upper(), str(row["timeframe"]).strip().lower())
        slots.setdefault(key, []).append(
            (str(row["id"]), sharpe if sharpe is not None else float("-inf"))
        )

    archived: list[dict] = []
    for (symbol, timeframe), members in slots.items():
        if len(members) <= 1:
            continue
        members.sort(key=lambda m: m[1], reverse=True)
        keep_id, keep_sharpe = members[0]
        for victim_id, victim_sharpe in members[1:]:
            entry = {
                "slot": f"{symbol} {timeframe}",
                "kept": keep_id,
                "kept_sharpe": None if keep_sharpe == float("-inf") else keep_sharpe,
                "archived": victim_id,
                "archived_sharpe": None if victim_sharpe == float("-inf") else victim_sharpe,
            }
            if not dry_run:
                try:
                    transition_stage(
                        strategy_id=victim_id,
                        target_stage="archived",
                        reason=(
                            f"Slot de-dup: {symbol} {timeframe} kept best-Sharpe "
                            f"incumbent {keep_id}"
                        ),
                        actor=actor,
                        force=True,
                    )
                except Exception:
                    _logging.getLogger("forven.policy").exception(
                        "dedupe_capital_slots: failed to archive %s", victim_id
                    )
                    continue
            archived.append(entry)
    if archived:
        _logging.getLogger("forven.policy").info(
            "dedupe_capital_slots: archived %d redundant slot occupant(s)%s",
            len(archived),
            " (dry run)" if dry_run else "",
        )
    return {"archived": archived, "dry_run": dry_run, "slots_examined": len(slots)}


def _queue_challenger_dethrone(
    *,
    conn,
    incumbent_id: str,
    incumbent_stage: str,
    challenger_id: str,
    challenger_sharpe: float | None,
    incumbent_sharpe: float | None,
) -> int | None:
    """Queue a dethrone recommendation because a superior challenger appeared.

    Tagged with trigger='superior_challenger' so the insufficient-evidence
    auto-clear leaves it alone. Deduped on any existing pending dethrone.
    """
    pending = conn.execute(
        """
        SELECT id FROM approvals
        WHERE approval_type = ?
          AND target_type = 'strategy'
          AND target_id = ?
          AND status = 'pending_approval'
        ORDER BY id DESC
        LIMIT 1
        """,
        (_DETHRONE_APPROVAL_TYPE, incumbent_id),
    ).fetchone()
    if pending:
        return int(pending["id"])

    recommended_target_stage = _resolve_dethrone_target_stage(incumbent_stage)
    approval_id = create_approval(
        approval_type=_DETHRONE_APPROVAL_TYPE,
        target_type="strategy",
        target_id=incumbent_id,
        requested_status=recommended_target_stage,
        status="pending_approval",
        actor="policy",
        reason=(
            f"Dethrone recommendation: challenger {challenger_id} (Sharpe {challenger_sharpe}) "
            f"materially beats incumbent {incumbent_id} (Sharpe {incumbent_sharpe}) on the same slot"
        ),
        payload={
            "strategy_id": incumbent_id,
            "current_stage": incumbent_stage,
            "trigger": "superior_challenger",
            "challenger_id": challenger_id,
            "challenger_sharpe": challenger_sharpe,
            "incumbent_sharpe": incumbent_sharpe,
            "recommended_action": "dethrone",
            "recommended_target_stage": recommended_target_stage,
            "operator_required": True,
        },
        owner="ceo",
        conn=conn,
    )
    conn.execute(
        """
        INSERT INTO strategy_events
            (strategy_id, from_state, to_state, actor, reason, created_at)
        VALUES
            (?, ?, ?, 'policy', ?, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
        """,
        (
            incumbent_id,
            incumbent_stage,
            incumbent_stage,
            f"Dethrone recommendation queued (approval #{approval_id}): superior challenger {challenger_id}",
        ),
    )
    return approval_id


def _maybe_auto_apply_dethrone(approval_id: int | None, settings: object) -> None:
    """Apply a challenger-driven dethrone immediately when auto-approval is on.

    Routes through the operator-approval code path (force=True, actor='ui'), which
    demotes the incumbent paper->gauntlet — reversible, so the former incumbent can
    re-promote. Failures are swallowed: the approval simply stays pending for review.
    """
    if not approval_id:
        return
    settings_dict = settings if isinstance(settings, dict) else {}
    # Default ON: in autonomous operation a frozen roster (occupied slots that can
    # never free) is itself a failure mode. The flag defaults True so existing
    # installs without the key still free slots; an operator can explicitly set it
    # False to require manual approval. Fully-autonomous mode (promotion_mode=='auto'
    # / auto_approve_promotions) also implies auto-dethrone.
    auto_dethrone = bool(settings_dict.get("auto_approve_dethrone", True))
    if not auto_dethrone:
        try:
            from forven.brain import _auto_approve_promotions_enabled
            auto_dethrone = bool(_auto_approve_promotions_enabled())
        except Exception:  # noqa: BLE001 - autonomy probe is best-effort
            auto_dethrone = False
    if not auto_dethrone:
        return
    try:
        from forven.control_plane.approvals import post_approve_approval
        from forven.control_plane.models import ApprovalDecisionBody

        post_approve_approval(
            int(approval_id),
            ApprovalDecisionBody(
                actor="policy:auto_dethrone",
                reason="Auto-approved challenger-driven dethrone (auto_approve_dethrone enabled)",
            ),
        )
    except Exception as exc:  # noqa: BLE001 - leave pending on any failure
        log.warning("Auto-approve dethrone failed for approval #%s: %s", approval_id, exc)


def _check_repeated_failure_auto_archive(
    strategy_id: str,
    gate: str,
    reason_code: str,
    reason_text: str,
):
    """Auto-archive non-paper stages; queue dethrone recommendations for paper/live stages."""
    try:
        # Evidence-ABSENCE rejections must never accumulate toward auto-archive:
        #   * no_metrics_error — the backtest never produced metrics (db-lock, timeout,
        #     process-restart, blocked import, codegen crash); phantom recovery and the
        #     transient-retry paths own this case.
        #   * artifacts_pending / stale_validation / missing_evidence — the gauntlet
        #     tests have not been run or persisted (or need a post-optimization re-run)
        #     YET. Evolution polls the gate up to 3x per cycle, so counting these would
        #     terminally archive in-flight strategies in ~2-5 cycles before their edge
        #     was ever measured. Genuine ran-and-failed outcomes keep their quality
        #     reason codes (wfa_reject, robustness_reject, ...) and still count.
        if reason_code in _EVIDENCE_ABSENCE_REASON_CODES:
            if gate == "paper" and reason_code == "insufficient_paper_evidence":
                # Low-frequency warm-up evidence should never create archive pressure,
                # and any stale paper dethrone recommendation should be retired.
                # (L-21: keyed on the dedicated reason code, not text prefixes.)
                with get_db() as conn:
                    cleared = _clear_pending_dethrone_recommendations_for_strategy(
                        conn,
                        strategy_id,
                        reason=(
                            "Auto-cleared stale dethrone recommendation: current paper failure "
                            "is insufficient evidence"
                        ),
                    )
                    if cleared:
                        conn.execute(
                            """
                            INSERT INTO strategy_events
                                (strategy_id, from_state, to_state, actor, reason, created_at)
                            VALUES
                                (?, 'paper', 'paper', 'policy', ?, strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'))
                            """,
                            (
                                strategy_id,
                                f"Auto-cleared {cleared} stale dethrone approval(s) while paper evidence is still insufficient",
                            ),
                        )
            return

        with get_db() as conn:
            strat_row = conn.execute(
                "SELECT stage, stage_changed_at FROM strategies WHERE id = ?", (strategy_id,)
            ).fetchone()
            if not strat_row:
                return
            current_stage = str(strat_row["stage"] or "").strip().lower()
            if current_stage in ("archived", "rejected"):
                return
            stage_changed_at = str(strat_row["stage_changed_at"] or "").strip()

            # Count how many times this strategy has failed this specific gate
            # in its current lifecycle stay. Historical failures from a prior
            # archived/recovered cycle should not instantly bury a strategy that
            # an operator intentionally sent back for reevaluation.
            count_sql = (
                "SELECT COUNT(*) as c FROM gate_rejections "
                "WHERE strategy_id = ? AND gate = ? AND reason_code = ?"
            )
            count_params: list[object] = [strategy_id, gate, reason_code]
            if stage_changed_at:
                count_sql += " AND datetime(created_at) >= datetime(?)"
                count_params.append(stage_changed_at)
            # L-21: warm-up "Insufficient paper ..." rejections are now classified
            # as reason_code='insufficient_paper_evidence' (an evidence-absence
            # code that early-returns above), so no reason_text NOT LIKE filtering
            # is needed here — the count is keyed on the genuine quality code.
            count_row = conn.execute(count_sql, tuple(count_params)).fetchone()
            failure_count = int(count_row["c"]) if count_row else 0

            if failure_count < _REPEATED_FAILURE_THRESHOLD:
                return

            if current_stage in _DETHRONE_MANUAL_STAGES:
                # Operator-owned (paper/live) strategies are frozen: do NOT auto-queue
                # a paper->gauntlet dethrone recommendation from background gate
                # re-evaluations. Legitimate demotion signals are operator action and
                # the decay_tracker paper_live_drift path (both untouched). Without
                # this, the repeated metric degradation that the param/metric lock now
                # prevents would otherwise have already filed spurious recs; this is
                # the belt-and-braces suppression at the rec source.
                from forven.brain import stage_is_param_locked

                if stage_is_param_locked(current_stage):
                    log.info(
                        "dethrone suppressed: %s at %s is operator-owned; "
                        "not auto-queuing dethrone rec after %d failures at %s/%s",
                        strategy_id, current_stage, failure_count, gate, reason_code,
                    )
                    return
                if _is_dethrone_cooldown_active(strategy_id):
                    return
                approval_id = _queue_dethrone_recommendation(
                    conn=conn,
                    strategy_id=strategy_id,
                    current_stage=current_stage,
                    gate=gate,
                    reason_code=reason_code,
                    reason_text=reason_text,
                    failure_count=failure_count,
                )
                if approval_id:
                    log.warning(
                        "DETHRONE RECOMMENDATION: %s queued approval #%s after %d failures at %s/%s",
                        strategy_id,
                        approval_id,
                        failure_count,
                        gate,
                        reason_code,
                    )
                    try:
                        log_activity(
                            "warning",
                            "policy",
                            f"Dethrone recommendation queued for {strategy_id}",
                            {
                                "approval_id": approval_id,
                                "strategy_id": strategy_id,
                                "current_stage": current_stage,
                                "gate": gate,
                                "reason_code": reason_code,
                                "failure_count": failure_count,
                                "threshold": _REPEATED_FAILURE_THRESHOLD,
                            },
                        )
                    except Exception:
                        pass
                return

        # Auto-archive — through the canonical lifecycle path (M-12 2026-06-09 audit).
        # The previous raw `UPDATE strategies SET stage='archived'` bypassed every
        # transition_stage protection: the canonical guard, pending agent_task +
        # active gauntlet workflow cancellation, stage_changed_at/updated_at, the
        # post-mortem narrative, brain_decisions outcome backfill and skill-outcome
        # closure — meaning the single most common automated death path was invisible
        # to the self-improvement loop and left zombie workflows behind.
        #
        # Deliberately OUTSIDE the counting connection above: transition_stage opens
        # its own get_db() writes. (The call is reachable from inside an outer
        # transition_stage via evaluate_promotion, but that outer conn is still
        # read-only/autocommit at gate-evaluation time — see the "Keep `conn`
        # read-only" comments in brain.transition_stage — so the nested write conn
        # cannot self-deadlock.)
        #
        # force=True with actor='auto_archive' (in brain._SYSTEM_FORCE_ACTORS): after
        # 5 genuine ran-and-failed quality rejections the archive must actually happen
        # rather than park behind ghost protection; the canonical guard still blocks
        # (auto_archive is neither decay_tracker nor a forced user actor) and the
        # skill-outcome closure still records (not a _USER_ACTOR).
        from forven.brain import transition_stage

        transition = transition_stage(
            strategy_id,
            "archived",
            reason=(
                f"Repeated failure: {gate}/{reason_code} failed {failure_count}x "
                f"(threshold: {_REPEATED_FAILURE_THRESHOLD})"
            ),
            actor="auto_archive",
            force=True,
        )
        if str(transition.get("to") or "").strip().lower() == "archived":
            log.warning(
                "AUTO-ARCHIVE: %s archived after %d failures at %s/%s",
                strategy_id, failure_count, gate, reason_code,
            )
        else:
            log.warning(
                "AUTO-ARCHIVE BLOCKED: %s kept in %s after %d failures at %s/%s — %s",
                strategy_id,
                transition.get("to"),
                failure_count,
                gate,
                reason_code,
                transition.get("blocked_reason") or "transition did not reach archived",
            )
    except Exception as exc:
        log.warning("Auto-archive check failed for %s: %s", strategy_id, exc)


def _implausible_metrics_reason(metrics: dict, config: dict) -> str | None:
    """Detect impossibly-good metrics (the signature of a lookahead/data leak):
    a Sharpe at/above the plausibility ceiling (or pegged at the +/-10 backtest
    clamp) or an absurd profit factor. Checks BOTH in-sample and out-of-sample,
    because a leak makes both slices uniformly amazing (which is exactly what the
    IS/OOS-gap overfit detector cannot catch).

    Robust to missing/None metrics: a strategy with no metrics returns None and
    is handled by the surrounding "no metrics" checks. Reads the ceilings from
    the quick_screen / gauntlet sub-configs (whichever is passed via ``config``)
    falling back to the wired DEFAULT_PIPELINE_CONFIG defaults.
    """
    if not isinstance(metrics, dict) or not metrics:
        return None

    # Resolve ceilings from whichever sub-config is in play (quick_screen at the
    # quick-screen gate, gauntlet at the gauntlet gate). Default to the larger
    # gauntlet/quick_screen wired defaults if absent.
    qs_defaults = DEFAULT_PIPELINE_CONFIG["quick_screen"]
    gate_qs = config.get("quick_screen", {}) if isinstance(config, dict) else {}
    gate_g = config.get("gauntlet", {}) if isinstance(config, dict) else {}

    def _ceiling(field: str, default: float) -> float:
        # Prefer an explicit value on either sub-config; fall back to the default.
        for src in (gate_qs, gate_g):
            if isinstance(src, dict) and src.get(field) is not None:
                return _coerce_float(src.get(field), default)
        return default

    max_sharpe = _ceiling("max_plausible_sharpe", float(qs_defaults["max_plausible_sharpe"]))
    max_pf = _ceiling("max_plausible_profit_factor", float(qs_defaults["max_plausible_profit_factor"]))

    # Reuse the same IS/OOS extraction the surrounding gate code uses.
    in_sample = _unwrap_metrics_dict(metrics.get("in_sample") or metrics.get("is") or {})
    out_of_sample = _unwrap_metrics_dict(metrics.get("out_of_sample") or metrics.get("oos") or {})
    # If neither distinct slice exists, treat the top-level blob as the sole slice
    # (this is the raw single-run backtest) so a leak that only writes top-level
    # metrics is still caught.
    if not in_sample and not out_of_sample:
        in_sample = metrics

    def _sharpe(section: dict) -> float | None:
        if not isinstance(section, dict):
            return None
        return _coerce_optional_float(section.get("sharpe", section.get("sharpe_ratio")))

    def _pf(section: dict) -> float | None:
        if not isinstance(section, dict):
            return None
        return _coerce_optional_float(section.get("profit_factor", section.get("pf")))

    # +/-10 is the backtest clamp (forven.strategies.backtest._MAX_ABS_RISK_RATIO):
    # a Sharpe within 0.01 of it was silently capped, not earned -- definitely a
    # leak/bug, rejected regardless of the configurable ceiling.
    clamp = 10.0

    for label, section in (("IS", in_sample), ("OOS", out_of_sample)):
        s = _sharpe(section)
        if s is not None:
            if abs(s) >= clamp - 0.01:
                pf = _pf(section)
                pf_disp = pf if pf is not None else float("nan")
                return (
                    f"Implausible metrics ({label} Sharpe {s:.1f} pegged at the "
                    f"+/-{clamp:.0f} backtest clamp / PF {pf_disp:.1f}) -- silently "
                    f"clamped, not earned; likely lookahead/data leak; rejected"
                )
            if abs(s) >= max_sharpe:
                pf = _pf(section)
                pf_disp = pf if pf is not None else float("nan")
                return (
                    f"Implausible metrics ({label} Sharpe {s:.1f} / PF {pf_disp:.1f}) "
                    f">= plausibility ceiling (Sharpe {max_sharpe:.1f}) -- "
                    f"likely lookahead/data leak; rejected"
                )
        pf = _pf(section)
        if pf is not None and pf >= max_pf:
            s_disp = s if s is not None else float("nan")
            return (
                f"Implausible metrics ({label} Sharpe {s_disp:.1f} / PF {pf:.1f}) "
                f">= plausibility ceiling (PF {max_pf:.1f}) -- "
                f"likely lookahead/data leak; rejected"
            )

    return None


def _evaluate_quick_screen_gate(strategy_id: str, config: dict) -> tuple[bool, str]:
    """Step 1 -> Step 2 gate: cheap triage on return/dd/sharpe with S00552 guardrails.

    S00552 GUARDRAILS (Hard Gates at Gauntlet Entry):
    1. IS/OOS Sharpe Gap Limit: Reject if gap > 1.5 points
    2. Pre-Gauntlet Robustness Gate: Require robustness ≥ 10/100 BEFORE gauntlet progression
    3. Profit Factor Floor: Require PF >= quick_screen.min_profit_factor (default 1.0)
       at BOTH IS and OOS stages
    4. Win Rate Trap Detection: Reject if WR > 60% but PF < 1.0
    """
    gate = config.get("quick_screen", {})
    _qs_defaults = DEFAULT_PIPELINE_CONFIG["quick_screen"]
    row = _load_strategy_row_for_gate(strategy_id)
    if not row:
        return False, "Strategy not found"

    metrics = _load_metrics_blob(row)
    if not metrics:
        return False, "No quick-screen metrics available"

    # Fast sanity check: reject strategies with zero trades (can't produce signals)
    total_trades = int(metrics.get("total_trades", 0) or 0)
    oos_data = metrics.get("out_of_sample") or metrics.get("oos") or {}
    oos_trades = int(oos_data.get("total_trades", oos_data.get("trades", 0)) or 0)
    if total_trades == 0 and oos_trades == 0:
        return False, "Quick screen reject: zero trades — strategy produces no signals in this market window"

    # Launch hardening: enforce the wired quick_screen.min_trades floor. The
    # gate previously rejected ONLY the zero/zero case, so the min_trades knob
    # (default 30) was dead code and a 5-trade luck strategy advanced to the
    # gauntlet — Sharpe/PF/win-rate are statistically meaningless below ~30
    # trades. Use the LARGER of the IS/OOS counts so a strategy with a healthy
    # primary backtest still passes when only the OOS slice is thin.
    min_trades_floor = int(
        _coerce_float(gate.get("min_trades", _qs_defaults["min_trades"]), _qs_defaults["min_trades"])
    )
    # The persisted top-level total_trades is the OOS-flattened count (the
    # canonical backtest blob writes top-level fields from the OOS slice), so
    # read the in-sample count explicitly too — otherwise the floor silently
    # demands ~min_trades OOS trades (~3.4x more total) and false-rejects a
    # well-sampled strategy with a thin OOS slice.
    _is_data = metrics.get("in_sample") or metrics.get("is") or {}
    is_trades = int(_is_data.get("total_trades", _is_data.get("trades", 0)) or 0)
    effective_trades = max(total_trades, oos_trades, is_trades)
    if min_trades_floor > 0 and effective_trades < min_trades_floor:
        return False, (
            f"Quick screen reject: {effective_trades} trades < {min_trades_floor} minimum "
            f"(metrics statistically meaningless below the floor)"
        )

    # === S00552 GUARDRAILS (Hard Gates for Gauntlet Admission) ===

    # Determine metrics evidence level (P1-1)
    in_sample = metrics.get("in_sample") or metrics.get("is") or {}
    out_of_sample = metrics.get("out_of_sample") or metrics.get("oos") or {}
    has_walk_forward = bool(metrics.get("walk_forward")) or bool(metrics.get("wfa"))

    if in_sample and out_of_sample:
        evidence_level = "walk_forward" if has_walk_forward else "validation_matrix"
    elif in_sample or out_of_sample:
        evidence_level = "single_run"
    else:
        evidence_level = "single_run"

    # P1-2: Require distinct IS/OOS evidence — NO fallback aliasing.
    # If no IS, use top-level metrics as IS (this is the raw backtest).
    if not in_sample:
        in_sample = metrics
    if not out_of_sample:
        # CRITICAL FIX: Do NOT alias IS as OOS. Run legacy triage only.
        log.warning(
            "S00552 [%s]: No distinct OOS evidence found. evidence_level=%s. "
            "Running legacy triage only — gauntlet admission blocked until walk-forward evidence exists.",
            strategy_id, evidence_level,
        )
        # Fall through to legacy triage checks below, but block gauntlet admission
        out_of_sample = {}  # Empty — will fail OOS-specific checks gracefully
    
    in_sample = _unwrap_metrics_dict(in_sample)
    out_of_sample = _unwrap_metrics_dict(out_of_sample)

    has_distinct_oos = bool(out_of_sample)

    # 1. IS Sharpe > 0.1 (hard gate)
    is_sharpe = float(in_sample.get("sharpe", in_sample.get("sharpe_ratio", 0.0)) or 0.0)
    if is_sharpe <= 0.1:
        return False, f"OVERFIT REJECT: IS Sharpe {is_sharpe:.2f} <= 0.1 (hard gate failed)"

    # 2. OOS Sharpe > -0.1
    oos_sharpe = float(out_of_sample.get("sharpe", out_of_sample.get("sharpe_ratio", 0.0)) or 0.0)
    if has_distinct_oos and oos_sharpe <= -0.1:
        return False, f"OVERFIT REJECT: OOS Sharpe {oos_sharpe:.2f} <= -0.1 (no OOS performance)"
    
    # === S00552 GUARDRAIL 1: IS/OOS Sharpe Gap Limit (absolute points, not ratio) ===
    sharpe_gap = is_sharpe - oos_sharpe
    if has_distinct_oos and sharpe_gap > 1.5:
        return False, f"S00552 REJECT: IS/OOS Sharpe gap {sharpe_gap:.2f} exceeds 1.5 limit (gauntlet entry blocked)"
    
    # === S00552 GUARDRAIL 2: Pre-Gauntlet Robustness Gate (≥ 25/100) ===
    robustness = _resolve_robustness_points(metrics)
    if robustness < 10.0:
        return False, f"S00552 REJECT: Robustness {robustness:.1f}/100 below 10 minimum (pre-gauntlet gate failed)"
    
    # === S00552 GUARDRAIL 3: Profit Factor Floor at BOTH IS and OOS ===
    # M-15 (2026-06-09 audit): honor the wired quick_screen.min_profit_factor
    # knob instead of a hardcoded 1.05 (the default preserves the old floor).
    min_pf_floor = _coerce_float(
        gate.get("min_profit_factor", _qs_defaults["min_profit_factor"]),
        _qs_defaults["min_profit_factor"],
    )
    is_pf = float(in_sample.get("profit_factor", in_sample.get("pf", 0.0)) or 0.0)
    oos_pf = float(out_of_sample.get("profit_factor", out_of_sample.get("pf", 0.0)) or 0.0)
    if is_pf < min_pf_floor:
        return False, f"S00552 REJECT: IS Profit Factor {is_pf:.2f} below {min_pf_floor:.2f} minimum"
    if has_distinct_oos and oos_pf < min_pf_floor:
        return False, f"S00552 REJECT: OOS Profit Factor {oos_pf:.2f} below {min_pf_floor:.2f} minimum"

    # === IMPLAUSIBLE-METRICS REJECT (lookahead/data-leak guard) ===
    # quick_screen is the universal entry every strategy passes, so this is the
    # primary catch for a future-bar leak: too-good Sharpe/PF on EITHER slice.
    reason = _implausible_metrics_reason(metrics, config)
    if reason:
        return False, reason

    # === S00552 GUARDRAIL 4: Win Rate Trap Detection (WR > 60% but PF < 1.0) ===
    win_rate = float(out_of_sample.get("win_rate", out_of_sample.get("winRate", 0.0)) or 0.0)
    min_pf = min(is_pf, oos_pf) if has_distinct_oos else is_pf
    if has_distinct_oos and win_rate > 60.0 and min_pf < 1.0:
        return False, f"S00552 REJECT: Win rate trap detected - WR {win_rate:.1f}% but PF {min_pf:.2f} < 1.0 (likely curve-fitted)"
    
    # Legacy: High win rate (>60%) strategies require higher standards
    if has_distinct_oos and win_rate > 60.0:
        if oos_pf <= 1.2:
            return False, f"OVERFIT REJECT: High win rate {win_rate:.1f}% requires OOS PF > 1.2, got {oos_pf:.2f}"
        if oos_sharpe <= 0:
            return False, f"OVERFIT REJECT: High win rate {win_rate:.1f}% requires positive OOS Sharpe, got {oos_sharpe:.2f}"
    
    # === ORIGINAL TRIAGE CHECKS (for backward compatibility) ===
    # Use OOS metrics when available, but fall back to top-level for missing fields.
    oos_metrics = _metrics_section(metrics, "out_of_sample", "oos")
    # Merge: OOS overrides top-level, but top-level provides missing values.
    target_metrics = dict(metrics)
    if isinstance(oos_metrics, dict) and oos_metrics:
        target_metrics.update({k: v for k, v in oos_metrics.items() if v is not None})

    total_return_pct = _to_percent_points(target_metrics.get("total_return_pct", 0.0))
    max_dd = _to_ratio(target_metrics.get("max_drawdown_pct", 1.0), 1.0)
    sharpe = float(target_metrics.get("sharpe", 0.0) or 0.0)

    # P1-6: Single-source defaults from DEFAULT_PIPELINE_CONFIG — no contradictory fallbacks.
    min_return = float(gate.get("min_total_return_pct", _qs_defaults["min_total_return_pct"]))
    max_dd_limit = float(gate.get("max_drawdown_pct", _qs_defaults["max_drawdown_pct"]))
    min_sharpe = float(gate.get("min_sharpe", _qs_defaults["min_sharpe"]))

    # P1-6: Log resolved thresholds in gate decisions
    log.debug(
        "Quick-screen resolved thresholds for %s: min_return=%.2f%%, max_dd=%.2f, min_sharpe=%.2f",
        strategy_id, min_return, max_dd_limit, min_sharpe,
    )

    if total_return_pct < min_return:
        return False, f"Quick screen return too low: {total_return_pct:.2f}% (minimum {min_return:.2f}%)"
    if max_dd > max_dd_limit:
        return False, f"Quick screen drawdown too high: {max_dd*100:.2f}% (maximum {max_dd_limit*100:.2f}%)"
    if sharpe < min_sharpe:
        return False, f"Quick screen Sharpe too low: {sharpe:.2f} (minimum {min_sharpe:.2f})"
    
    return True, "Passed quick screen triage with S00552 guardrails"

def _mc_dd_floor_reject(mc_payload: dict, mc_dd_limit: float) -> str | None:
    """Monte-Carlo 95th-percentile drawdown SAFETY FLOOR (S00552).

    Returns a reject message if the *measured* tail drawdown exceeds the limit,
    else None.

    H-2: when no dd key is present in the payload (e.g. a display-proxy
    monte_carlo blob — verdict_engine relabels a max-drawdown check as
    "monte_carlo" and carries no dd field — which is exactly what the live paper
    roster including S08808 has), this returns None rather than defaulting to a
    999 sentinel and rejecting on garbage ("DD 99900.0% exceeds 50% limit"). A
    genuine 0.0 is a real measurement and IS enforced; only true absence skips.
    """
    if not isinstance(mc_payload, dict):
        return None
    raw = mc_payload.get("max_dd_p95")
    if raw is None:
        raw = mc_payload.get("p95_dd")
    if raw is None:
        raw = mc_payload.get("drawdown_95th")
    mc_max_dd = _coerce_optional_float(raw)
    if mc_max_dd is not None and mc_max_dd > mc_dd_limit:
        return (
            f"S00552 REJECT: Monte Carlo 95th percentile DD {mc_max_dd * 100:.1f}% "
            f"exceeds {mc_dd_limit * 100:.0f}% limit"
        )
    return None


def _log_advisory_robustness_paper(strategy_id: str, verdict_payloads: dict, rob_thresholds: dict) -> None:
    """Log (don't block) the strict-live robustness checks that the lean paper gate
    no longer enforces, so the demotions stay observable."""
    try:
        notes: list[str] = []
        mc = verdict_payloads.get("monte_carlo") or verdict_payloads.get("mc")
        if isinstance(mc, dict):
            pct = mc.get("percentile_score", mc.get("robustness_pct"))
            mc_min = float(rob_thresholds.get("monte_carlo_percentile_min", 0.65))
            if pct is not None and float(pct) < mc_min:
                notes.append(f"MC percentile {float(pct):.0%}<{mc_min:.0%}")
        cost = verdict_payloads.get("cost_stress")
        if isinstance(cost, dict):
            ss = cost.get("stressed_sharpe")
            cmin = float(rob_thresholds.get("cost_stress_min_sharpe", 0.3))
            if ss is not None and float(ss) < cmin:
                notes.append(f"cost-stressed Sharpe {float(ss):.2f}<{cmin:.2f}")
        regime = verdict_payloads.get("regime_split")
        if isinstance(regime, dict):
            prof = regime.get("profitable_regime_pct", regime.get("profitable_pct"))
            rmin = float(rob_thresholds.get("regime_split_profitable_min", 0.50))
            if prof is not None and float(prof) < rmin:
                notes.append(f"regimes profitable {float(prof):.0%}<{rmin:.0%}")
        if notes:
            log.info(
                "paper gate (lean): %s would fail strict-live robustness [%s] — advisory only, "
                "enforced at paper->live",
                strategy_id, "; ".join(notes),
            )
    except Exception:
        pass


def _strict_robustness_reject(strategy_id: str, row, metrics: dict, config: dict) -> str | None:
    """Strict robustness battery for the LIVE (capital) gate.

    Re-checks the stored gauntlet payloads at full strictness — the criteria the
    lean paper gate demoted to advisory: WFA IS->OOS degradation, absolute OOS
    Sharpe, OOS trade count, Monte-Carlo percentile, cost-stress survival, and
    regime consistency. Returns a rejection reason, or None if all clear.
    Read-only / best-effort (may run inside the gate's write txn).
    """
    gate = config.get("gauntlet", {})
    rob = config.get("robustness_thresholds", {})
    try:
        verdict_payloads, _ = _extract_gauntlet_verdict_payloads(strategy_id, row, metrics)
    except Exception:
        return None
    if not isinstance(verdict_payloads, dict):
        return None

    wfa = verdict_payloads.get("walk_forward")
    if isinstance(wfa, dict):
        deg = wfa.get("degradation", wfa.get("sharpe_degradation"))
        if deg is not None and float(deg) > float(gate.get("wfa_max_degradation", 0.35)):
            return (
                f"Live gate: walk-forward IS->OOS degradation {float(deg):.0%} exceeds "
                f"{float(gate.get('wfa_max_degradation', 0.35)):.0%} limit"
            )
        oos_tr = wfa.get("total_oos_trades", wfa.get("oos_trades"))
        if oos_tr is not None and int(float(oos_tr)) < int(gate.get("wfa_min_oos_trades", 20)):
            return (
                f"Live gate: walk-forward OOS trades {int(float(oos_tr))} below "
                f"{int(gate.get('wfa_min_oos_trades', 20))} minimum"
            )
        oos_sh = wfa.get("avg_oos_sharpe", wfa.get("oos_sharpe"))
        if oos_sh is not None and float(oos_sh) < float(gate.get("wfa_min_oos_sharpe", 0.3)):
            return (
                f"Live gate: walk-forward OOS Sharpe {float(oos_sh):.2f} below "
                f"{float(gate.get('wfa_min_oos_sharpe', 0.3)):.2f} floor"
            )

    mc = verdict_payloads.get("monte_carlo") or verdict_payloads.get("mc")
    if isinstance(mc, dict):
        pct = mc.get("percentile_score", mc.get("robustness_pct"))
        mc_min = float(rob.get("monte_carlo_percentile_min", 0.65))
        if pct is not None and float(pct) < mc_min:
            return f"Live gate: Monte-Carlo percentile {float(pct):.0%} below {mc_min:.0%} target"

    cost = verdict_payloads.get("cost_stress")
    cost_unusable = (
        not isinstance(cost, dict)
        or bool(cost.get("non_required_failure"))
        or (cost.get("stressed_sharpe") is None and cost.get("degradation_pct") is None)
    )
    # FAIL CLOSED on a missing/errored cost_stress — but ONLY when the strategy actually
    # ran the gauntlet (walk_forward present, since it is a required test). cost_stress is
    # non-required at the gauntlet->paper gate (Default preset "achievable paper, strict
    # live"), so an ERRORED probe (gauntlet._non_required_skip) leaves no usable survival
    # result here; without this guard the cost-survival checks below silently no-op and a
    # strategy whose edge does NOT survive 2x fees/slippage could graduate to REAL MONEY.
    # Gating on wfa avoids over-blocking direct/test promotions that carry no gauntlet
    # validations at all (cost would also be absent there, but for a benign reason). This
    # runs only when live_strict_robustness_enabled, so failing closed is safe: the
    # strategy stays in paper, no capital at risk, until a clean cost-stress result exists.
    if isinstance(wfa, dict) and cost_unusable:
        return (
            "Live gate: cost-stress survival could not be verified (no usable cost_stress "
            "result) — failing closed before real capital"
        )
    # Gate on SURVIVAL (positive stressed Sharpe) AND bounded degradation, not just an
    # absolute floor.
    if isinstance(cost, dict) and not cost_unusable:
        ss = cost.get("stressed_sharpe")
        cmin = float(rob.get("cost_stress_min_sharpe", 0.3))
        if ss is not None and float(ss) < cmin:
            return f"Live gate: cost-stressed Sharpe {float(ss):.2f} below {cmin:.2f}"
        deg_pct = cost.get("degradation_pct")
        max_cost_deg = float(rob.get("cost_stress_max_degradation_pct", 60.0))
        if deg_pct is not None and float(deg_pct) > max_cost_deg:
            return f"Live gate: cost-stress degradation {float(deg_pct):.0f}% exceeds {max_cost_deg:.0f}%"

    regime = verdict_payloads.get("regime_split")
    if isinstance(regime, dict):
        prof = regime.get("profitable_regime_pct", regime.get("profitable_pct"))
        rmin = float(rob.get("regime_split_profitable_min", 0.50))
        if prof is not None and float(prof) < rmin:
            return f"Live gate: only {float(prof):.0%} of regimes profitable (minimum {rmin:.0%})"

    return None


# DEFAULT values for the editable config["safety_floors"] — the absolute anti-bypass
# floors clamped onto the capital-bearing gauntlet->PAPER entry gate. They bound how
# far a relaxed preset / custom config / automated caller can soften the ->paper path
# (a relaxed value is clamped to these; where lower==stricter, e.g. drawdown, the cap
# is from above). UPDATED 2026-06-24: these are NO LONGER immutable — they are the
# DEFAULTS for config["safety_floors"], which is fully operator-editable from Settings
# (set any to 0 to remove that rail). Entry to PAPER risks no real capital; the
# real-money floors are safety_floors.live_*.
_PAPER_GATE_FLOORS = {
    "min_robustness_score": 0.0,
    "mc_max_dd_p95": 0.50,  # ceiling: tail DD can never be relaxed above 50%
    "wfa_fold_pass_rate_min": 0.20,
    "param_jitter_pass_rate_min": 0.30,
    "min_trades": 3,
}


def _evaluate_gauntlet_gate(strategy_id: str, config: dict) -> tuple[bool, str]:
    """Step 2 -> Step 3 gate: require robustness gauntlet score and S00552 test evidence.
    
    S00552 GAUNTLET GUARDRAILS:
    - Walk-Forward Mandate: Minimum 3-fold WFA with ALL folds passing
    - Monte Carlo MaxDD: Ensure 95th percentile DD < 25% before progression
    """
    gate = config.get("gauntlet", {})
    # Editable absolute floors (defaults from _PAPER_GATE_FLOORS) clamp how far a
    # relaxed preset / custom config can soften this ->paper gate.
    floors = dict(_PAPER_GATE_FLOORS)
    floors.update({k: v for k, v in (config.get("safety_floors") or {}).items() if k in floors})
    required = gate.get("required_tests", []) or []
    required_tests = {_canonicalize_gauntlet_verdict_test(t) for t in required if str(t).strip()}
    enforce_all_verdict_tests = not required_tests

    def _verdict_test_required(test_name: str) -> bool:
        return enforce_all_verdict_tests or _canonicalize_gauntlet_verdict_test(test_name) in required_tests

    row = _load_strategy_row_for_gate(strategy_id)
    if not row:
        return False, "Strategy not found"

    artifact_counts = _load_gauntlet_artifact_counts(strategy_id)
    if artifact_counts["optimization"] <= 0 and artifact_counts["walk_forward"] <= 0:
        return (
            False,
            "Gauntlet requires at least one persisted optimization or walk-forward run before promotion to paper",
        )
    ordering_ok, ordering_msg = _check_artifact_ordering(strategy_id, list(required_tests) if required_tests else None)
    if not ordering_ok:
        return False, ordering_msg
    freshness_ok, freshness_msg = _check_validation_freshness(strategy_id, list(required_tests) if required_tests else None)
    if not freshness_ok:
        return False, freshness_msg

    metrics = _load_metrics_blob(row)
    if not metrics:
        return False, "No gauntlet metrics available"

    target_metrics = _unwrap_metrics_dict(metrics.get("out_of_sample", metrics))

    # === IMPLAUSIBLE-METRICS REJECT (defense-in-depth; primary catch is quick_screen) ===
    # A future-bar leak makes both IS and OOS uniformly amazing, slipping past the
    # IS/OOS-gap overfit detector — reject too-good Sharpe/PF before scoring.
    reason = _implausible_metrics_reason(metrics, config)
    if reason:
        return False, reason

    # === TRADE-COUNT FLOOR (capital gate — UNCONDITIONAL) ============================
    # The paper stage commits real capital, so a statistically meaningful trade
    # sample is a HARD prerequisite — independent of which validation artifacts
    # happen to exist. The trade-count floor previously lived ONLY inside the Monte
    # Carlo block below, so a strategy that reached this gate without an MC artifact
    # (e.g. a non-workflow api/system ->paper promotion) was never checked, and a
    # handful of <10-trade strategies slipped through to paper. Enforce it here for
    # every ->paper promotion, clamped to the editable safety_floors.min_trades floor
    # (default 3) so a relaxed gauntlet.min_trades config cannot soften it below that
    # operator-set rail. (The old immutable 30 is now a low, tunable "achievable paper"
    # floor — entry to paper risks no real capital.)
    sample_min_trades = max(
        int(_coerce_float(gate.get("min_trades", floors["min_trades"]), floors["min_trades"])),
        floors["min_trades"],
    )
    sample_trades = _resolve_full_sample_trade_count(metrics)
    if sample_trades is None:
        return False, (
            "Paper gate: strategy has no trade-count metric — cannot verify a "
            "sufficient sample for capital allocation"
        )
    if int(sample_trades) < sample_min_trades:
        return False, (
            f"Paper gate reject: {int(sample_trades)} trades < {sample_min_trades} minimum "
            f"(insufficient sample for capital allocation)"
        )

    min_return = float(gate.get("min_total_return_pct", 0.0))
    max_dd_limit = float(gate.get("max_drawdown_pct", 0.30))
    min_win_rate = float(gate.get("min_win_rate", 40.0))

    has_total_return = target_metrics.get("total_return_pct") is not None or target_metrics.get("total_return") is not None
    has_max_dd = target_metrics.get("max_drawdown_pct") is not None
    has_win_rate = target_metrics.get("win_rate") is not None

    if has_total_return:
        total_return_pct = _to_percent_points(target_metrics.get("total_return_pct", target_metrics.get("total_return", 0.0)))
        if total_return_pct < min_return:
            return False, f"Gauntlet return too low: {total_return_pct:.2f}% (minimum {min_return:.2f}%)"
    if has_max_dd:
        max_dd = _to_ratio(target_metrics.get("max_drawdown_pct", 1.0), 1.0)
        if max_dd > max_dd_limit:
            return False, f"Gauntlet drawdown too high: {max_dd*100:.2f}% (maximum {max_dd_limit*100:.2f}%)"
    # Win-rate is NOT a quality gate by default: momentum/breakout strategies win
    # ~30-40% of the time and profit on payoff ratio, so a hard win-rate floor
    # auto-rejects an entire (legitimate) strategy family. Profit factor / OOS edge
    # is the right screen. Off by default; re-enableable via gauntlet_enforce_win_rate.
    if has_win_rate and bool(gate.get("gauntlet_enforce_win_rate", False)):
        win_rate_pct = _to_percent_points(target_metrics.get("win_rate", 0.0))
        if win_rate_pct < min_win_rate:
            return False, f"Gauntlet win rate too low: {win_rate_pct:.1f}% (minimum {min_win_rate:.1f}%)"

    robustness = _resolve_robustness_points(metrics)

    # If the config narrows hard requirements, score robustness from those
    # required verdicts instead of letting optional/stale validation failures
    # permanently depress old strategy rows.
    if required_tests or robustness <= 0:
        verdict_payloads_early, _overall_status_early = _extract_gauntlet_verdict_payloads(strategy_id, row, metrics)
        scoring_payloads = verdict_payloads_early
        if required_tests:
            scoring_payloads = {
                test_name: payload
                for test_name, payload in verdict_payloads_early.items()
                if test_name in required_tests
            }
        if scoring_payloads:
            test_count = len(scoring_payloads)
            passed_count = sum(
                1 for v in scoring_payloads.values()
                if isinstance(v, dict) and not _verdict_payload_failed(v)
            )
            if test_count > 0:
                required_robustness = round((passed_count / test_count) * 100.0, 1)
                robustness = max(robustness, required_robustness)
                log.info(
                    "Computed robustness for %s from %s test results: %d/%d passed = %.1f/100",
                    strategy_id,
                    "required" if required_tests else "all",
                    passed_count,
                    test_count,
                    required_robustness,
                )

    min_robustness = max(float(gate.get("min_robustness_score", 50)), floors["min_robustness_score"])  # F2 floor
    if robustness < min_robustness:
        return False, f"Gauntlet robustness too low: {robustness:.1f}/100 (minimum {min_robustness:.1f})"

    # === S00552 GAUNTLET: WFA and Monte Carlo enforcement ===
    verdict_payloads, overall_status = _extract_gauntlet_verdict_payloads(strategy_id, row, metrics)

    # P25-4: Load robustness thresholds early (needed for WFA pass-rate band below)
    rob_thresholds = config.get("robustness_thresholds", {})

    # P1-9: Explicit, configurable WFA thresholds with stricter targets.
    wfa_thresholds = {
        "max_degradation": float(gate.get("wfa_max_degradation", 0.35)),
        "min_oos_trades": int(gate.get("wfa_min_oos_trades", 20)),
        "min_oos_sharpe": float(gate.get("wfa_min_oos_sharpe", 0.3)),
        "min_folds": int(gate.get("wfa_min_folds", 2)),
    }

    wfa_payload = verdict_payloads.get("walk_forward")
    if wfa_payload:
        # F4(b): at the paper gate the WFA fold-consistency floor fires whenever
        # walk_forward actually ran — narrowing required_tests must NOT disable a
        # ran-but-failed safety check (the membership-gating was a bypass lever).
        enforce_wfa = True
        wfa_folds = wfa_payload.get("folds", wfa_payload.get("n_folds", 0))
        # Use pass_rate (numeric 0-1) for fold evaluation, NOT the boolean 'passed'
        # flag which reflects the overall verdict (may fail for non-fold reasons like
        # negative IS Sharpe average).
        wfa_pass_rate = wfa_payload.get("pass_rate", 1.0)
        if isinstance(wfa_pass_rate, bool):
            wfa_pass_rate = 1.0 if wfa_pass_rate else 0.0
        wfa_pass_rate = float(wfa_pass_rate)
        if enforce_wfa and wfa_folds < wfa_thresholds["min_folds"]:
            return False, f"S00552 REJECT: Walk-forward has {wfa_folds} folds, requires minimum {wfa_thresholds['min_folds']}"
        # Single source of truth for the WFA fold-pass-rate floor: the same
        # robustness_thresholds.wfa_fold_pass_rate_min the composite scorer uses
        # (_validation_row_passed), so the money gate and the rank score can't
        # silently drift. (Previously this gate read wfa_pass_rate_band[0]=0.30
        # while the scorer used wfa_fold_pass_rate_min=0.40 — two thresholds for
        # the same property.)
        min_pass_rate = max(
            float(
                rob_thresholds.get(
                    "wfa_fold_pass_rate_min",
                    DEFAULT_PIPELINE_CONFIG["robustness_thresholds"]["wfa_fold_pass_rate_min"],
                )
            ),
            floors["wfa_fold_pass_rate_min"],  # F2 floor
        )
        if enforce_wfa and wfa_pass_rate < min_pass_rate:
            return False, f"S00552 REJECT: Walk-forward pass rate {wfa_pass_rate:.0%} below {min_pass_rate:.0%} minimum"

        # PAPER GATE IS LEAN: the OOS *consistency* check above (folds + fold pass
        # rate) is the paper-stage hard gate. The absolute-Sharpe / degradation /
        # OOS-trade-count sub-checks are STRICT-LIVE criteria — they're regime traps
        # at the paper stage (testnet measures real edge anyway) and are enforced in
        # the paper->live gate via _strict_robustness_reject. Emit thresholds for
        # auditability.
        wfa_payload["_resolved_thresholds"] = wfa_thresholds
    
    # S00552: Monte Carlo MaxDD - 95th percentile DD check (configurable via gauntlet.mc_max_dd_p95)
    mc_payload = verdict_payloads.get("monte_carlo") or verdict_payloads.get("mc")
    mc_dd_limit = min(float(gate.get("mc_max_dd_p95", 0.40)), floors["mc_max_dd_p95"])  # F2 ceiling
    # Hard SAFETY FLOORS (baseline-trade count + 95th-percentile drawdown) fire whenever
    # Monte Carlo actually ran — NOT only when monte_carlo is in required_tests. A measured
    # 60% tail drawdown is a real risk signal regardless of operator config. The soft
    # percentile *calibration band* below stays gated behind required_tests.
    if mc_payload:
        mc_trades = mc_payload.get("n_trades", mc_payload.get("trade_count", mc_payload.get("total_trades")))
        mc_min_trades = max(int(gate.get("min_trades", DEFAULT_PIPELINE_CONFIG["gauntlet"]["min_trades"]) or 1), floors["min_trades"])  # F2 floor
        if mc_trades is not None and int(float(mc_trades or 0)) < mc_min_trades:
            return False, (
                f"S00552 REJECT: Monte Carlo baseline has {int(float(mc_trades or 0))} trades, "
                f"requires minimum {mc_min_trades}"
            )
        mc_dd_reject = _mc_dd_floor_reject(mc_payload, mc_dd_limit)
        if mc_dd_reject:
            return False, mc_dd_reject

    # Param jitter pass rate check — KEPT as a paper hard gate. This is the genuine
    # overfitting probe (does the edge survive parameter perturbation), orthogonal to
    # the OOS/regime checks, so it stays even at the lean paper stage.
    jitter_payload = verdict_payloads.get("param_jitter")
    if jitter_payload:  # F4(b): fire whenever param_jitter ran, regardless of required_tests
        jitter_rate = jitter_payload.get("pass_rate", jitter_payload.get("stable_pct"))
        jitter_min = max(float(rob_thresholds.get("param_jitter_pass_rate_min", 0.60)), floors["param_jitter_pass_rate_min"])  # F2 floor
        if jitter_rate is not None and float(jitter_rate) < jitter_min:
            return False, (
                f"P25-4 REJECT: Parameter jitter pass rate {float(jitter_rate):.1%} "
                f"below {jitter_min:.0%} target"
            )

    # MC percentile, cost-stress Sharpe, and regime-split profitability are
    # STRICT-LIVE criteria (regime-trap / underpowered at the paper stage) — they no
    # longer hard-block ->paper. They are computed/surfaced for observation and
    # enforced in the paper->live gate via _strict_robustness_reject. (The MC tail-
    # drawdown SAFETY floor above still fires at paper regardless.) Log advisory.
    _log_advisory_robustness_paper(strategy_id, verdict_payloads, rob_thresholds)

    # Deflated Sharpe Ratio — optimizer selection-bias guard. OPT-IN: the DSR is
    # computed and surfaced for observation regardless, but only REJECTS here when
    # robustness_thresholds.deflated_sharpe_gate_enabled is on, so it can be
    # calibrated before it blocks strategies. Read-only + best-effort (the gate may
    # run inside a write txn — never let an advisory metric stall promotion).
    if bool(rob_thresholds.get("deflated_sharpe_gate_enabled", False)):
        try:
            from forven.gauntlet.deflated_sharpe import compute_strategy_dsr

            dsr_info = compute_strategy_dsr(strategy_id)
        except Exception:
            dsr_info = None
        dsr_val = dsr_info.get("dsr") if isinstance(dsr_info, dict) else None
        if dsr_val is not None:
            min_dsr = float(rob_thresholds.get("min_deflated_sharpe", 0.90))
            if float(dsr_val) < min_dsr:
                return False, (
                    f"DSR REJECT: Deflated Sharpe {float(dsr_val):.2f} below {min_dsr:.2f} "
                    f"target (likely an optimizer selection artifact across "
                    f"{dsr_info.get('n_trials')} trials)"
                )

    # S00552: Profit Factor enforcement at gauntlet (in addition to quick_screen).
    # M-15 (2026-06-09 audit): configurable via gauntlet.min_oos_profit_factor
    # (default 1.05 preserves the previously-hardcoded floor).
    min_oos_pf = _coerce_float(
        gate.get("min_oos_profit_factor", DEFAULT_PIPELINE_CONFIG["gauntlet"]["min_oos_profit_factor"]),
        DEFAULT_PIPELINE_CONFIG["gauntlet"]["min_oos_profit_factor"],
    )
    oos_pf = float(target_metrics.get("profit_factor", 0.0) or 0.0)
    if oos_pf < min_oos_pf:
        return False, f"S00552 REJECT: Gauntlet OOS Profit Factor {oos_pf:.2f} below {min_oos_pf:.2f} minimum"

    if required_tests:
        if not verdict_payloads:
            verdict_payloads, overall_status = _extract_gauntlet_verdict_payloads(strategy_id, row, metrics)
        available_tests = set(verdict_payloads)
        if not available_tests:
            missing = ", ".join(sorted(required_tests))
            return False, f"Gauntlet missing verdict evidence for required tests: {missing}"
        missing = sorted(required_tests.difference(available_tests))
        if missing:
            return False, f"Gauntlet missing required verdict tests: {', '.join(missing)}"
        failing = sorted(
            test_name
            for test_name in required_tests
            if _verdict_payload_failed(verdict_payloads.get(test_name))
        )
        if failing:
            return False, f"Gauntlet required verdict tests failed: {', '.join(failing)}"
    elif overall_status == "fail":
        return False, "Gauntlet overall verdict failed"

    # === Promotion Readiness Gates (configurable via pipeline settings) ===
    ps = _load_pipeline_settings()
    warnings_list: list[str] = []

    def _gate_check(enabled_key: str, required_key: str, check_fn, *args) -> tuple[bool, str] | None:
        if not ps.get(enabled_key, True):
            return None  # Skipped
        ok, detail, *_ = check_fn(*args)
        if ok:
            return None  # Passed
        if ps.get(required_key, True):
            return False, detail  # Hard block
        warnings_list.append(detail)
        return None  # Soft warning

    # Multi-TF sweep
    result = _gate_check("gate_multi_tf_sweep_enabled", "gate_multi_tf_sweep_required",
                         _check_multi_tf_backtests, strategy_id,
                         int(ps.get("gate_multi_tf_min_timeframes", 3)))
    if result:
        return result

    # Real artifact rows for all required tests
    result = _gate_check("gate_require_artifact_rows_enabled", "gate_require_artifact_rows_required",
                         _check_artifact_rows_exist, strategy_id, required)
    if result:
        return result

    suffix = ""
    if warnings_list:
        suffix = f" (warnings: {'; '.join(warnings_list)})"

    return True, f"Passed robustness gauntlet with S00552 guardrails{suffix}"


def _snapshot_graduation_baseline(strategy_id: str, metrics: dict, paper_pnls: list[float]):
    """P4-4: Snapshot paper metrics at graduation for later drift comparison."""
    try:
        from statistics import mean, pstdev
        snapshot = {
            "graduated_at": datetime.now(timezone.utc).isoformat(),
            "backtest_sharpe": float(metrics.get("sharpe", 0.0) or 0.0),
            "backtest_max_dd": float(metrics.get("max_drawdown_pct", 0.0) or 0.0),
            "backtest_pf": float(metrics.get("profit_factor", 0.0) or 0.0),
            "paper_trade_count": len(paper_pnls),
            "paper_avg_pnl": round(mean(paper_pnls), 6) if paper_pnls else 0.0,
            "paper_pnl_std": round(pstdev(paper_pnls), 6) if len(paper_pnls) >= 2 else 0.0,
            "paper_total_return": round(sum(paper_pnls), 6),
        }
        kv_set(f"graduation_baseline:{strategy_id}", snapshot)
        log.info("P4-4: Saved graduation baseline for %s", strategy_id)
    except Exception as exc:
        log.warning("Failed to snapshot graduation baseline for %s: %s", strategy_id, exc)


def _evaluate_paper_gate(strategy_id: str, config: dict) -> tuple[bool, str]:
    """Step 3 -> Step 4 gate: forward paper proof (duration, sample, return, drawdown).

    S00152 OVERFITTING GUARDRAILS (thresholds wired via paper_trading.* settings):
    - OOS>>IS Flag: Block if OOS Sharpe > ``max_oos_is_ratio`` x IS Sharpe
      (default 1.5 — indicates a lucky/overfit OOS window)
    - PF Threshold: Require PF >= ``min_profit_factor_live`` (default 1.5) for
      live; PF below ``pf_position_reduction_threshold`` (default 2.0) passes
      with a 50% position-size reduction
    - Extended Paper Trading: ``min_closed_trades`` (default 50) AND
      ``min_paper_days`` (default 14) — both required
    - Robustness Check: Must have positive live return in paper before promotion
    """
    gate = config.get("paper_trading", {})
    _pt_defaults = DEFAULT_PIPELINE_CONFIG["paper_trading"]
    # Editable absolute live (real-money) floors. Defaults are safe; an operator can
    # lower them from Settings (full control, no fixed backstop — loosen with care).
    _floors = config.get("safety_floors") or {}
    row = _load_strategy_row_for_gate(strategy_id)
    if not row:
        return False, "Strategy not found"

    # STRICT-LIVE robustness battery: the criteria the lean paper gate demoted to
    # advisory (WFA degradation / absolute OOS Sharpe / OOS trade count, Monte-Carlo
    # percentile, cost-stress survival, regime consistency) are ENFORCED here, before
    # capital — operationalizing "achievable paper, strict live". Wired on/off.
    if bool(gate.get("live_strict_robustness_enabled", True)):
        _strict_metrics = _load_metrics_blob(row)
        if _strict_metrics:
            _strict_reason = _strict_robustness_reject(strategy_id, row, _strict_metrics, config)
            if _strict_reason:
                return False, _strict_reason

    # L-19 (2026-06-09 audit): this is the live-money gate — FAIL CLOSED when the
    # paper stage entry time is unknown. Previously a missing/unparseable
    # stage_changed_at silently skipped the min-duration check AND unbounded the
    # trade-evidence window to all history.
    stage_since = str(row["stage_changed_at"] or "").strip()
    min_days = int(gate.get("min_paper_days", _pt_defaults["min_paper_days"]))
    started = None
    if stage_since:
        try:
            started = datetime.fromisoformat(stage_since)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
        except Exception:
            started = None
    if started is None:
        return False, (
            "Paper stage entry time unknown (stage_changed_at missing or unparseable) — "
            "failing closed: paper duration and trade-evidence window cannot be verified"
        )
    days_in_stage = (datetime.now(timezone.utc) - started).days
    if days_in_stage < min_days:
        return False, f"Insufficient paper duration: {days_in_stage}/{min_days} days"

    params: list[object] = [strategy_id, stage_since]
    where_since = " AND datetime(closed_at) >= datetime(?)"

    with get_db() as conn:
        trade_rows = conn.execute(
            "SELECT COALESCE(net_pnl_pct, pnl_pct) AS pnl_pct FROM trades "
            "WHERE COALESCE(strategy_id, strategy) = ? "
            "AND status = 'CLOSED' "
            "AND pnl_pct IS NOT NULL "
            "AND LOWER(COALESCE(execution_type, '')) LIKE 'paper%'"
            + where_since,
            tuple(params),
        ).fetchall()

    pnls = [float(r["pnl_pct"]) for r in trade_rows if r["pnl_pct"] is not None]
    live = compute_live_metrics(pnls)
    total_trades = int(live.get("total_trades", 0))
    total_return = float(live.get("total_return_pct", 0.0))
    max_dd = float(live.get("max_drawdown_pct", 0.0))

    # S00152: Extended Paper Trading - min trades AND min days (both required)
    # Editable absolute live floors (safe defaults; fully operator-tunable).
    min_trades = max(
        int(gate.get("min_closed_trades", _pt_defaults["min_closed_trades"])),
        int(_floors.get("live_min_closed_trades", 3)),
    )
    min_return = float(gate.get("min_total_return_pct", _pt_defaults["min_total_return_pct"]))
    max_dd_limit = min(
        float(gate.get("max_drawdown_pct", _pt_defaults["max_drawdown_pct"])),
        float(_floors.get("live_max_drawdown_pct", 0.25)),
    )

    if total_trades < min_trades:
        return False, f"Insufficient paper sample: {total_trades}/{min_trades} closed trades"

    # === S00152: Load and validate backtest metrics for overfitting guardrails ===
    # Only evaluate these once the strategy has accumulated sufficient forward paper evidence.
    metrics = _load_metrics_blob(row)

    # S00152: OOS>>IS Flag - Block if OOS Sharpe far exceeds IS Sharpe (overfit/
    # lucky OOS window). M-14 (2026-06-09 audit): real metrics nest Sharpe under
    # in_sample/out_of_sample — the old flat-only is_sharpe/oos_sharpe read made
    # this check dead code. M-15: ratio limit wired via paper_trading.max_oos_is_ratio.
    is_sharpe, oos_sharpe = _extract_is_oos_sharpe(metrics)
    max_oos_is_ratio = _coerce_float(
        gate.get("max_oos_is_ratio", _pt_defaults["max_oos_is_ratio"]),
        _pt_defaults["max_oos_is_ratio"],
    )
    if is_sharpe > 0 and oos_sharpe > 0:
        oos_is_ratio = oos_sharpe / is_sharpe
        if oos_is_ratio > max_oos_is_ratio:
            return False, (
                f"S00152 REJECT: OVERFITTING RISK - OOS Sharpe {oos_sharpe:.2f} > "
                f"{max_oos_is_ratio:.2f}x IS Sharpe {is_sharpe:.2f} (ratio: {oos_is_ratio:.2f})"
            )

    # S00152: PF Threshold — M-15 (2026-06-09 audit): floors wired via
    # paper_trading.min_profit_factor_live (hard floor, default 1.5) and
    # paper_trading.pf_position_reduction_threshold (below it -> 50% sizing,
    # default 2.0). Defaults preserve the previously-hardcoded live-money values.
    min_pf_live = _coerce_float(
        gate.get("min_profit_factor_live", _pt_defaults["min_profit_factor_live"]),
        _pt_defaults["min_profit_factor_live"],
    )
    pf_reduction_below = _coerce_float(
        gate.get("pf_position_reduction_threshold", _pt_defaults["pf_position_reduction_threshold"]),
        _pt_defaults["pf_position_reduction_threshold"],
    )
    profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
    oos_section = _metrics_section(metrics, "out_of_sample", "oos")
    oos_profit_factor = _coerce_float(
        metrics.get("oos_profit_factor", oos_section.get("profit_factor", profit_factor))
    )
    # Use OOS profit factor if available, otherwise use general PF
    eval_pf = oos_profit_factor if oos_profit_factor > 0 else profit_factor

    pf_position_reduction = False
    if eval_pf < min_pf_live:
        return False, (
            f"S00152 REJECT: Profit Factor {eval_pf:.2f} below "
            f"{min_pf_live:.2f} minimum for live deployment"
        )
    if eval_pf < pf_reduction_below:
        pf_position_reduction = True
    
    # S00152: Robustness Check - Must have positive live return in paper before promotion
    if total_return <= 0:
        return False, f"S00152 REJECT: Paper return not positive: {total_return:.2f}% (must be > 0 for promotion)"
    
    if total_return <= min_return:
        return False, f"Paper return not positive enough: {total_return:.2f}% (minimum {min_return:.2f}%)"
    if max_dd >= max_dd_limit:
        return False, f"Paper drawdown too high: {max_dd*100:.2f}% (maximum {max_dd_limit*100:.2f}%)"

    # Launch hardening (define "winning"): the PF floor above proves HISTORICAL
    # edge; the live-money gate must ALSO require the FORWARD paper trades
    # themselves to show real edge — not merely a non-negative return.
    # compute_live_metrics() already derived these from the paper PnLs. Each
    # floor is opt-out via 0 (so "achievable paper" deployments can disable it).
    paper_sharpe = float(live.get("sharpe", 0.0) or 0.0)
    paper_pf = float(live.get("profit_factor", 0.0) or 0.0)
    min_paper_sharpe = _coerce_float(
        gate.get("min_paper_sharpe", _pt_defaults.get("min_paper_sharpe", 0.0)),
        _pt_defaults.get("min_paper_sharpe", 0.0),
    )
    min_paper_pf = _coerce_float(
        gate.get("min_profit_factor_paper", _pt_defaults.get("min_profit_factor_paper", 0.0)),
        _pt_defaults.get("min_profit_factor_paper", 0.0),
    )
    # The forward Sharpe is a t-stat (mean/stdev * sqrt(n)) and is only
    # meaningful when the paper PnLs actually disperse. compute_live_metrics()
    # returns 0.0 for a degenerate zero-variance series (every trade identical),
    # which would otherwise falsely fail a deterministically-positive strategy —
    # so the significance floor is skipped when there is no dispersion (the PF
    # floor below still binds). Real paper data always disperses.
    has_return_dispersion = len(pnls) > 1 and pstdev(pnls) > 1e-12
    if min_paper_sharpe > 0 and has_return_dispersion and paper_sharpe < min_paper_sharpe:
        return False, (
            f"S00152 REJECT: forward paper Sharpe {paper_sharpe:.2f} below "
            f"{min_paper_sharpe:.2f} minimum (no demonstrated forward edge)"
        )
    if min_paper_pf > 0 and paper_pf < min_paper_pf:
        return False, (
            f"S00152 REJECT: forward paper profit factor {paper_pf:.2f} below "
            f"{min_paper_pf:.2f} minimum (no demonstrated forward edge)"
        )

    # === Paper-to-Live Optimization Gates ===
    ps = _load_pipeline_settings()

    for enabled_key, required_key, check_fn in [
        ("paper_live_gate_optimization_enabled", "paper_live_gate_optimization_required",
         _check_optimization_exists),
        ("paper_live_gate_params_applied_enabled", "paper_live_gate_params_applied_required",
         _check_params_applied),
        ("paper_live_gate_confirmation_backtest_enabled", "paper_live_gate_confirmation_backtest_required",
         _check_confirmation_backtest),
    ]:
        if not ps.get(enabled_key, True):
            continue
        ok, detail, *_ = check_fn(strategy_id)
        if not ok and ps.get(required_key, True):
            return False, detail

    allocation_cap = _resolve_live_allocation_pct(days_in_stage, config, strategy_id=strategy_id)

    # P4-4: Snapshot paper metrics at graduation for drift comparison
    _snapshot_graduation_baseline(strategy_id, metrics, pnls)

    # S00152: Apply PF-based position reduction if applicable
    if pf_position_reduction:
        allocation_cap = allocation_cap * 0.5
        return True, f"Passed paper gate with S00152 PF warning (50% size reduction, live cap: {allocation_cap:.0f}%)"

    return True, f"Passed paper forward-proof gate (live graduated cap: {allocation_cap:.0f}% allocation)"

def _evaluate_deploy_gate(strategy_id: str, config: dict) -> tuple[bool, str]:
    """Backward-compatible alias for older callers expecting deploy gate."""
    return _evaluate_paper_gate(strategy_id, config)

def compute_live_metrics(pnls: list[float]) -> dict:
    """Shared utility to compute performance metrics from a PnL list."""
    total_trades = len(pnls)
    if total_trades <= 0:
        return {
            "total_trades": 0,
            "total_return_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate": 0.0,
            "sharpe": 0.0,
        }

    gross_profit = sum(p for p in pnls if p > 0)
    gross_loss = abs(sum(p for p in pnls if p < 0))
    if gross_loss <= 1e-12:
        profit_factor = 999.0 if gross_profit > 0 else 0.0
    else:
        profit_factor = gross_profit / gross_loss

    wins = sum(1 for p in pnls if p > 0)
    win_rate = wins / total_trades

    equity = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for pnl in pnls:
        equity *= max(0.0, 1.0 + pnl)
        if equity > peak:
            peak = equity
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)

    sharpe = 0.0
    if total_trades > 1:
        stdev = pstdev(pnls)
        if stdev > 1e-12:
            sharpe = (mean(pnls) / stdev) * math.sqrt(total_trades)

    return {
        "total_trades": total_trades,
        "total_return_pct": (equity - 1.0) * 100.0,
        "profit_factor": float(profit_factor),
        "max_drawdown_pct": float(max_drawdown),
        "win_rate": float(win_rate),
        "sharpe": float(sharpe),
    }
