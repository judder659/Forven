"""Settings schema, defaults, coercion and validation.

ARCH-06: the declarative half of the settings subsystem, moved VERBATIM out of
``forven.api_core``. What lives here is everything that has NO storage side
effect — the default payloads, the pure normalizers/coercers, the per-section
key + numeric-bound schema, the validator, and the audit differ.

What deliberately did NOT move (and stays in ``forven.api_core``): every
function that reads or writes KV — ``_load_settings_payload``,
``_save_settings_payload``, the secrets/api-key/pipeline loaders, and
``_apply_settings_section`` itself. Those resolve ``kv_get``/``kv_set``/
``kv_set_many``/``get_db`` through ``forven.api_core``'s module globals, and the
existing test suite monkeypatches exactly those bindings on ``api_core`` to
prove settings mutations are atomic (see
tests/test_settings_atomic_mutations.py, tests/test_secret_storage.py,
tests/test_research_contract.py). Moving them would silently detach those
patches from the code under test — the refactor would look green while the
atomicity proofs stopped proving anything. Splitting the storage half needs
those tests updated in the same change.

``forven.api_core`` re-exports every name defined here, so existing importers
(``forven.policy``, the routers, the tests) keep working untouched.
"""

import logging
import threading

from fastapi import HTTPException

from forven.ai import normalize_provider_and_model
from forven.db import _now
from forven.providers.discovery import (
    _DEFAULT_AGENT_MODEL_KEYS,
    _SUPPORTED_AUTH_PROVIDERS,
    _agent_model_option_key,
)
from forven.throughput_policy import THROUGHPUT_DEFAULTS

log = logging.getLogger("forven.api")


_SETTINGS_STORAGE_KEY = "forven:settings"
_SETTINGS_SECRET_STORAGE_KEY = "forven:settings:secrets"
_SETTINGS_API_KEYS_STORAGE_KEY = "forven:settings:api-keys"
_SETTINGS_PIPELINE_STORAGE_KEY = "forven:pipeline:settings"

# Serializes the FULL read->apply->diff->audit->save sequence of a settings
# mutation. Settings are the control plane (trading mode, risk caps, gate
# thresholds); a mutation loads the current blob, mutates it, diffs old-vs-new
# for the audit log, and writes several KV keys. Two concurrent PUTs without
# serialization race that read-modify-write: one edit is lost entirely and the
# audit diff can attribute one request's changes to the other's actor. A
# process-level lock (single-process app, uvicorn workers=1) makes each mutation
# atomic against every other mutation so both edits land and each audit entry
# reflects exactly its own request. Held only around the in-memory
# apply+diff+persist critical section — post-save side-effect hooks (scheduler
# overrides, daemon-state cleanup) run outside it.
_SETTINGS_MUTATION_LOCK = threading.RLock()

# Single source of truth for the default backtest window (calendar days). This ONE
# setting governs every automatic backtest that doesn't carry an explicit start/end:
# quick-screen, gauntlet timeframe-sweep/optimization/confirmation, walk-forward,
# the cost-stress rerun, and the evolution/crucible validation matrix. Every fallback
# below references this so a missing key can never silently shrink the window (the old
# scattered 365/30 fallbacks did exactly that, contradicting the saved 730 default).
DEFAULT_BACKTEST_DURATION_DAYS = 730

_DEFAULT_SETTINGS_PAYLOAD = {
    "exchange": "hyperliquid",
    "trading_mode": "paper",
    "initial_capital": 10000,
    "hyperliquid_wallet": "",
    "hyperliquid_api_address": "",
    "hyperliquid_has_key": False,
    "hyperliquid_testnet": True,
    "max_position_size_pct": 10,
    "max_risk_per_trade_pct": 10,
    "recovery_emergency_stop_max_pct": 5,
    "max_daily_loss": 200,
    "max_daily_loss_pct": 2,
    "max_drawdown_pct": 30,
    "min_risk_reward_ratio": 0,
    "risk_fee_bps": 4.5,
    "risk_slippage_bps": 2.0,
    "max_concurrent_positions": 5,
    "paper_max_concurrent_positions": 0,
    "live_books_enabled": False,
    "hyperliquid_long_book_address": "",
    "hyperliquid_short_book_address": "",
    "hyperliquid_use_cross_margin": False,
    # SLICE-BASE-1: size live opens off the combined long+short pool (a strategy
    # deploys one direction at a time). Off = routed-book-only (half slices).
    "live_slice_combined_books": True,
    # Propr mirror per-trade risk, whole percent of the member's slice.
    "propr_mirror_risk_pct": 2,
    "liq_distance_warn_pct": 15,
    "liq_distance_critical_pct": 7,
    "cooldown_after_loss_hours": 0,
    "agent_model_keys": _DEFAULT_AGENT_MODEL_KEYS,
    "backup_ai_provider": "none",
    "backup_ai_model": "",
    "discord_bot_token_configured": False,
    "discord_webhook_configured": False,
    "notification_level": "all",
    "notify_on_entry": True,
    "notify_on_exit": True,
    "notify_daily_summary": True,
    "notify_health_reports": True,
    "notify_errors": True,
    "scanner_execution_enabled": True,
    "relaxed_trade_filters_enabled": False,
    "strict_regime_gating": True,
    "regime_min_confidence": 0.3,
    "allow_unknown_regime_strategies": False,
    "regime_gate_mode": "observe",
    "regime_gate_block_long": "TREND_DOWN,HIGH_VOL",
    "regime_gate_block_short": "",
    "regime_gate_min_confidence": 0.6,
    "self_healing_enabled": True,
    "auto_restart_on_crash": True,
    "maintenance_start_hour": None,
    "maintenance_end_hour": None,
    "data_refresh_seconds": 60,
    "throughput_auto_scheduler_control": True,
    "adaptive_pipeline_throughput_enabled": False,
    "pipeline_target_clear_hours": 6,
    # Throughput knobs (shared single-source defaults; the scheduler fallbacks
    # and the "balanced" throughput preset reference the same constants).
    "ideation_interval_minutes": THROUGHPUT_DEFAULTS["ideation_interval_minutes"],
    "coding_interval_minutes": THROUGHPUT_DEFAULTS["coding_interval_minutes"],
    "testing_interval_minutes": THROUGHPUT_DEFAULTS["testing_interval_minutes"],
    "graduation_interval_minutes": THROUGHPUT_DEFAULTS["graduation_interval_minutes"],
    "scanner_signal_interval_minutes": 5,
    "scanner_execution_interval_minutes": 5,
    "scanner_allow_direct_market_fetch": True,
    # Market-data exchange for paper data/prices/chart. 'binance' (the lead exchange,
    # default) makes paper trade on the SAME real series the backtest validates on;
    # 'hyperliquid' reverts to the HL feed. Execution stays in-app regardless.
    "market_data_source": "binance",
    "daemon_candle_cache_refresh_seconds": 90,
    "paper_test_mode_enabled": False,
    "paper_test_high_activity_enabled": False,
    "paper_test_bypass_gates_enabled": False,
    "paper_test_local_execution_only": False,
    "pipeline_assignments_per_cycle": 3,
    "pipeline_drain_mode": True,
    "backtest_matrix_workers": 4,
    # Process-wide cap on concurrent backtest SUBPROCESSES (memory ceiling all the
    # parallel pipeline levers queue on). See forven/strategies/concurrency.py.
    "backtest_subprocess_budget": THROUGHPUT_DEFAULTS["backtest_subprocess_budget"],
    # Gauntlet workflows advanced concurrently per tick (1 = serial drain).
    "gauntlet_drain_workers": THROUGHPUT_DEFAULTS["gauntlet_drain_workers"],
    "pipeline_saturation_threshold": 100,
    "pipeline_resume_threshold": 60,
    "pipeline_drain_max_seconds": 300,
    "pipeline_gate_failure_archive_attempts": 3,
    "gauntlet_auto_quick_screen_enabled": True,
    "gauntlet_quick_screen_max_attempts": 3,
    "gauntlet_step_stale_minutes": 30,
    "agent_task_claim_limit": THROUGHPUT_DEFAULTS["agent_task_claim_limit"],
    "brain_task_claim_limit": 12,
    # Soft cap on the pending brain_invoke queue before the scheduler prunes
    # (generic pings first, routine dispatches preserved; a hard ceiling backstops).
    "brain_queue_max_pending": 15,
    "code_strategy_requires_approval": False,
    "auto_approve_code_edits": False,
    "auto_approve_promotions": False,
    # GO-LIVE-1: even with auto_approve_promotions/promotion_mode=auto, a
    # paper→live promotion requires an explicit operator confirmation with a
    # notional ceiling — unless this is deliberately flipped on (dangerous).
    "allow_auto_live_promotion": False,
    # When a challenger materially beats an incumbent occupying a capital slot,
    # auto-apply the dethrone so the slot frees without operator action. Default
    # ON for autonomous operation — reversible (the incumbent is demoted
    # paper->gauntlet, not archived). See policy._maybe_auto_apply_dethrone.
    "auto_approve_dethrone": True,
    # When a hypothesis graduates and its per-cell-best becomes canonical, enqueue
    # the gauntlet paper-promotion gate for it (the robustness/required-test floor
    # still applies — it is NOT a direct transition). Default OFF: graduation stays
    # a label until the operator opts in. See hypothesis_graduation.graduate_hypothesis.
    "canonical_auto_deploy_enabled": False,
    # When True, capital slots hold ONE strategy per symbol/timeframe: the duplicate
    # tournament, paper slot-guard, capital-slot dedupe, and paper WIP cap all apply.
    # Default OFF: every strategy that passes the gauntlet is promoted to paper with
    # no per-slot competition and no cap. See policy._paper_slot_competition_enabled.
    "paper_slot_competition_enabled": False,
    "task_stale_recovery_minutes": 10,
    "health_checks_enabled": True,
    "rolling_backtest_days": 30,
    "walkforward_months": 6,
    "walkforward_folds": 5,
    "regime_detection_enabled": True,
    "alert_on_degradation_pct": 20,
    "backtest_fee_bps": 4.5,
    "backtest_slippage_bps": 2.0,
    # Fallback leverage when a strategy declares no `leverage` param. ONE default
    # shared by the gauntlet confirmation/robustness backtests, the execution-profile
    # selection, and the live/paper scanner so leverage-sensitive sizing matches
    # (the parity invariant). 1x = unlevered; operator-editable.
    "default_leverage": 1.0,
    "backtest_timeframe": "1h",
    "backtest_symbol": "BTC/USDT",
    # DEFAULT backtest window (calendar days, ending now). Used directly by ad-hoc /
    # manual backtests, and as the fallback any PER-STAGE window below inherits when it
    # is left at 0. 730d so slower timeframes (4h) reach a meaningful trade sample and
    # the WFA OOS folds span >1 market regime. Settings > Lab > "Default backtest window".
    "backtest_duration_days": DEFAULT_BACKTEST_DURATION_DAYS,
    # PER-STAGE backtest windows (calendar days). Each automated pipeline stage that
    # runs a backtest has its OWN tunable window so e.g. quick-screen can be short while
    # walk-forward spans years. Resolved via stage_backtest_duration_days(). Default 0 =
    # "inherit the Default backtest window" above — so out of the box every stage tracks
    # whatever backtest_duration_days is set to (behaviour-preserving). Set a positive
    # number of days to give that stage its own independent horizon.
    "quick_screen_duration_days": 0,
    "timeframe_sweep_duration_days": 0,
    "optimization_duration_days": 0,
    "confirmation_duration_days": 0,
    "walk_forward_duration_days": 0,
    "cost_stress_duration_days": 0,
    "evolution_duration_days": 0,
    # When enabled, backtests deduct cumulative perp funding from each trade's
    # PnL and refuse to promote strategies whose funding data was incomplete.
    "backtest_include_funding": True,
    "walkforward_cv_method": "rolling",
    "walkforward_train_ratio": 0.7,
    "walkforward_purge_gap": 0,
    "walkforward_embargo_pct": 0,
    "walkforward_objective": "sharpe_ratio",
    "walkforward_n_trials": 50,
    "remote_engine_enabled": False,
    "remote_engine_url": "http://127.0.0.1:9050",
    "remote_engine_data_root": "",
    "setup_wizard_completed_at": None,
    # Strict mode for the agent run_shell command guard (forven.sandbox.shell_guard).
    # Off by default; run_shell itself is also disabled by default. When True,
    # non-critical findings (high/medium/low) fail closed instead of warn-allow.
    # Backend-only — there is no UI control for this.
    "sandbox_shell_guard_strict": False,
    # Per-turn tool-round cap for the in-app assistant chat AND deepdive
    # sessions (each round = one full model call whose input grows with every
    # prior round, and with actions enabled each round can create real pipeline
    # objects). On hitting the cap the turn now lands softly (forced no-tools
    # final answer) instead of erroring. Bounded 2-40 at read time.
    "assistant_max_tool_rounds": 12,
    "updated_at": _now(),
}

# Only Polygon is wired to a consumer (forven.config.get_polygon_api_key ->
# polygon_client / data layer). The Tiingo/FRED/CoinGecko/Alpaca key fields were
# never read by any code, so they were removed from the Settings UI and dropped
# here too. Any previously-stored keys still round-trip via get_settings_api_keys'
# fallback loop, so nothing is lost.
_DEFAULT_API_KEY_SOURCES = ("polygon",)

_PIPELINE_STAGE_WIP_CAPS = {
    "paper": {
        "mode_key": "paper_wip_cap_mode",
        "cap_key": "paper_wip_cap",
        "default": 20,
    },
}
_PIPELINE_WIP_CAP_UNLIMITED_VALUES = {"", "0", "none", "null", "unlimited", "off", "disabled"}
_DEFAULT_GRAVEYARD_STRATEGY_LIMIT = 500
_GRAVEYARD_STRATEGY_STATUSES = {"archived", "rejected", "backtest_failed", "graveyard", "trash"}

_DEFAULT_PIPELINE_SETTINGS = {
    "version": 1,
    "autopilot_enabled": True,
    "autopilot_worker_concurrency": 4,
    "autopilot_generation_batch_size": 50,
    "autopilot_scan_symbol": "BTC/USDT",
    "autopilot_scan_timeframe": "1h",
    "autopilot_scan_symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    "autopilot_scan_timeframes": ["1h", "4h", "1d"],
    "autopilot_indicator_groups": ["trend", "momentum", "volatility"],
    "promotion_mode": "auto",
    # DB maintenance retention windows (days). 0 disables pruning for that table.
    # Consumed by forven.maintenance.run_db_maintenance via this same payload.
    "retention_backtest_trash_days": 14,
    "retention_activity_log_days": 90,
    "retention_scanner_results_days": 30,
    "retention_gate_rejections_days": 30,
    "maintenance_vacuum_enabled": False,
    "min_backtest_trades": 30,
    "min_sharpe_ratio": 0.5,
    "max_drawdown_pct": 40,
    "min_profit_factor": 1.0,
    # Aligned with the canonical gate store (forven:pipeline_thresholds /
    # DEFAULT_PIPELINE_CONFIG.paper_trading) so the optional readiness-gate
    # layer can't diverge from the active paper->live gate if its gate_*_enabled
    # toggles are ever turned on. The active gate reads pipeline_thresholds.
    "min_paper_days": 14,
    "max_paper_divergence_pct": 30,
    "min_paper_trades": 50,
    "min_paper_sharpe": 0.5,
    "paper_wip_cap_mode": "capped",
    "paper_wip_cap": 20,
    "graveyard_strategy_limit_mode": "capped",
    "graveyard_strategy_limit": _DEFAULT_GRAVEYARD_STRATEGY_LIMIT,
    "validation_recent_window_enabled": False,
    "validation_recent_window_months": 12,
    "validation_cost_stress_enabled": False,
    "validation_cost_stress_fee_multiplier": 2.0,
    "validation_cost_stress_slippage_multiplier": 2.0,
    "validation_min_recent_sharpe": 0.0,
    "validation_max_recent_drawdown_pct": 70.0,
    "validation_min_cost_stress_sharpe": -0.25,
    "validation_max_cost_stress_drawdown_pct": 85.0,
    "gate_min_trades_enabled": False,
    "gate_min_trades_required": False,
    "gate_min_sharpe_enabled": False,
    "gate_min_sharpe_required": False,
    "gate_max_drawdown_enabled": False,
    "gate_max_drawdown_required": False,
    "gate_min_profit_factor_enabled": False,
    "gate_min_profit_factor_required": False,
    "gate_min_paper_days_enabled": False,
    "gate_min_paper_days_required": False,
    "gate_min_paper_trades_enabled": False,
    "gate_min_paper_trades_required": False,
    "gate_min_paper_sharpe_enabled": False,
    "gate_min_paper_sharpe_required": False,
    "gate_max_paper_divergence_enabled": False,
    "gate_max_paper_divergence_required": False,
    "gate_recent_window_enabled": False,
    "gate_recent_window_required": False,
    "gate_cost_stress_enabled": False,
    "gate_cost_stress_required": False,
    "failed_retention_hours": 72,
    "autopilot_nuke_noise_enabled": False,
    "autopilot_nuke_noise_dry_run": True,
    "autopilot_survivor_min_tier": "strong",
    "ranking_top_n": 10,
    "ranking_metric": "sharpe_ratio",
    "created_by": "system",
    # --- Gauntlet Promotion Readiness Gates ---
    # Multi-timeframe sweep: require backtests across N distinct timeframes
    "gate_multi_tf_sweep_enabled": True,
    "gate_multi_tf_sweep_required": True,
    "gate_multi_tf_min_timeframes": 3,
    "gate_sweep_timeframes": ["15m", "1h", "4h", "1d"],
    # Optimization evidence belongs inside the gauntlet before robustness tests.
    "gate_optimization_required_enabled": True,
    "gate_optimization_required_required": True,
    # Optimized params are applied to the strategy container before robustness tests.
    "gate_params_applied_enabled": True,
    "gate_params_applied_required": True,
    # Confirmation backtest validates the optimized defaults before robustness starts.
    "gate_confirmation_backtest_enabled": True,
    "gate_confirmation_backtest_required": True,
    # Artifact ordering/freshness ensure robustness tests are run on optimized defaults.
    "gate_artifact_ordering_enabled": True,
    "gate_artifact_ordering_required": True,
    "gate_validation_freshness_enabled": True,
    "gate_validation_freshness_required": True,
    # Real artifact rows: require actual backtest_results rows, not just verdict blobs
    "gate_require_artifact_rows_enabled": True,
    "gate_require_artifact_rows_required": True,
    # --- Paper-to-Live Gates ---
    # Paper trading metric checks (informational readiness display)
    "paper_live_gate_paper_duration_enabled": True,
    "paper_live_gate_paper_duration_required": True,
    "paper_live_gate_paper_trades_enabled": True,
    "paper_live_gate_paper_trades_required": True,
    "paper_live_gate_paper_return_enabled": True,
    "paper_live_gate_paper_return_required": True,
    "paper_live_gate_paper_drawdown_enabled": True,
    "paper_live_gate_paper_drawdown_required": True,
    # Optimization must be completed before graduating from paper to live
    "paper_live_gate_optimization_enabled": False,
    "paper_live_gate_optimization_required": False,
    # Optimized params must be applied to strategy before going live
    "paper_live_gate_params_applied_enabled": False,
    "paper_live_gate_params_applied_required": False,
    # Confirmation backtest with optimized params before going live
    "paper_live_gate_confirmation_backtest_enabled": False,
    "paper_live_gate_confirmation_backtest_required": False,
}


def _default_settings_payload() -> dict:
    payload = dict(_DEFAULT_SETTINGS_PAYLOAD)
    payload["updated_at"] = _now()
    payload["research_settings"] = _default_research_settings_payload()
    payload["data_engine_settings"] = _default_data_engine_settings_payload()
    return payload


def _default_data_engine_settings_payload() -> dict:
    from forven.dataeng.settings import default_data_engine_settings_payload

    return default_data_engine_settings_payload()


def _merge_data_engine_settings_payload(value) -> dict:
    from forven.dataeng.settings import merge_data_engine_settings_payload

    return merge_data_engine_settings_payload(value)


def _deep_merge_dicts(base: dict, incoming: dict) -> dict:
    """Recursively merge ``incoming`` over ``base`` without mutating either.

    Nested dicts merge key-by-key (incoming leaves win); every other type is
    replaced wholesale. Used by section handlers that accept PARTIAL nested
    payloads (the settings UI sends only the edited leaves), so editing one
    nested leaf can never reset its stored siblings back to defaults.
    """
    merged = dict(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _default_research_settings_payload() -> dict:
    from forven.research_contract import default_research_settings

    return default_research_settings()


def _merge_research_settings_payload(value) -> dict:
    defaults = _default_research_settings_payload()
    if not isinstance(value, dict):
        return defaults

    def _merge_nested(default_value, current_value):
        if isinstance(default_value, dict):
            merged_nested = dict(default_value)
            if isinstance(current_value, dict):
                for nested_key, nested_value in current_value.items():
                    if nested_key in merged_nested:
                        merged_nested[nested_key] = _merge_nested(merged_nested[nested_key], nested_value)
                    else:
                        merged_nested[nested_key] = nested_value
            return merged_nested
        if isinstance(default_value, list):
            return list(current_value) if isinstance(current_value, list) else list(default_value)
        return current_value if current_value is not None else default_value

    merged: dict = {}
    for key, default_value in defaults.items():
        current_value = value.get(key)
        merged[key] = _merge_nested(default_value, current_value)

    for key, current_value in value.items():
        if key not in merged:
            merged[key] = current_value
    return merged


def _default_pipeline_settings_payload() -> dict:
    payload = dict(_DEFAULT_PIPELINE_SETTINGS)
    payload["created_at"] = _now()
    payload["created_by"] = "system"
    return payload


def _normalize_pipeline_wip_cap_mode(value: object, fallback: str = "capped") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _PIPELINE_WIP_CAP_UNLIMITED_VALUES:
        return "unlimited"
    if normalized in {"capped", "cap", "limited", "limit"}:
        return "capped"
    return fallback if fallback in {"capped", "unlimited"} else "capped"


def _normalize_pipeline_wip_cap_value(value: object, fallback: int) -> int:
    if value is None:
        return max(1, int(fallback))
    if isinstance(value, str) and value.strip().lower() in _PIPELINE_WIP_CAP_UNLIMITED_VALUES:
        return max(1, int(fallback))
    try:
        parsed = int(value) if isinstance(value, (int, float)) else int(str(value).strip())
    except Exception:
        parsed = int(fallback)
    return max(1, parsed)


def _normalize_pipeline_wip_cap_payload(payload: dict) -> dict:
    for stage_config in _PIPELINE_STAGE_WIP_CAPS.values():
        mode_key = str(stage_config["mode_key"])
        cap_key = str(stage_config["cap_key"])
        default_cap = int(stage_config["default"])
        payload[mode_key] = _normalize_pipeline_wip_cap_mode(
            payload.get(mode_key),
            str(_DEFAULT_PIPELINE_SETTINGS.get(mode_key) or "capped"),
        )
        payload[cap_key] = _normalize_pipeline_wip_cap_value(
            payload.get(cap_key),
            default_cap,
        )
    return payload


def _normalize_graveyard_strategy_limit_mode(value: object, fallback: str = "capped") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in _PIPELINE_WIP_CAP_UNLIMITED_VALUES:
        return "unlimited"
    if normalized in {"capped", "cap", "limited", "limit"}:
        return "capped"
    return fallback if fallback in {"capped", "unlimited"} else "capped"


def _normalize_graveyard_strategy_limit_value(value: object, fallback: int = _DEFAULT_GRAVEYARD_STRATEGY_LIMIT) -> int:
    if value is None:
        return max(1, int(fallback))
    if isinstance(value, str) and value.strip().lower() in _PIPELINE_WIP_CAP_UNLIMITED_VALUES:
        return max(1, int(fallback))
    try:
        parsed = int(value) if isinstance(value, (int, float)) else int(str(value).strip())
    except Exception:
        parsed = int(fallback)
    return max(1, parsed)


def _normalize_graveyard_strategy_limit_payload(payload: dict) -> dict:
    payload["graveyard_strategy_limit_mode"] = _normalize_graveyard_strategy_limit_mode(
        payload.get("graveyard_strategy_limit_mode"),
        str(_DEFAULT_PIPELINE_SETTINGS.get("graveyard_strategy_limit_mode") or "capped"),
    )
    payload["graveyard_strategy_limit"] = _normalize_graveyard_strategy_limit_value(
        payload.get("graveyard_strategy_limit"),
        _DEFAULT_GRAVEYARD_STRATEGY_LIMIT,
    )
    return payload


def _pipeline_wip_cap_kv_items(payload: dict) -> dict:
    """Compute the ``pipeline:wip_cap:*`` KV entries a payload implies (no write)."""
    items: dict = {}
    for stage, stage_config in _PIPELINE_STAGE_WIP_CAPS.items():
        mode_key = str(stage_config["mode_key"])
        cap_key = str(stage_config["cap_key"])
        if _normalize_pipeline_wip_cap_mode(payload.get(mode_key)) == "unlimited":
            items[f"pipeline:wip_cap:{stage}"] = "unlimited"
        else:
            items[f"pipeline:wip_cap:{stage}"] = _normalize_pipeline_wip_cap_value(
                payload.get(cap_key),
                int(stage_config["default"]),
            )
    return items


def _normalize_agent_model_key(raw: str) -> str | None:
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw:
        return None
    # Split on the FIRST colon only: provider:model_id. The model_id may itself
    # contain colons — OpenRouter free models are "vendor/model:free" — so we
    # must NOT reject a model_id that still contains a colon (that dropped every
    # OpenRouter :free key on save, reverting the Models-tab checkbox).
    provider, _, model_id = raw.partition(":")
    provider = provider.strip().lower()
    model_id = model_id.strip()
    if not provider or not model_id:
        return None
    provider, normalized_model_id = normalize_provider_and_model(provider, model_id)
    if provider not in _SUPPORTED_AUTH_PROVIDERS:
        return None
    return _agent_model_option_key(provider, normalized_model_id)


def _coerce_agent_model_keys(value) -> list[str]:
    if value is None:
        return list(_DEFAULT_AGENT_MODEL_KEYS)
    if not isinstance(value, list):
        return list(_DEFAULT_AGENT_MODEL_KEYS)

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        key = _normalize_agent_model_key(str(item))
        if key is None or key in seen:
            continue
        normalized.append(key)
        seen.add(key)
    return normalized


def _coerce_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "y"}:
            return True
        if normalized in {"0", "false", "no", "off", "n"}:
            return False
    return default


def _coerce_optional_int(value, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        return int(value)
    cleaned = str(value).strip()
    if not cleaned:
        return default
    try:
        return int(float(cleaned))
    except Exception:
        return default


def _coerce_bounded_int(value, default: int, lower: int, upper: int) -> int:
    parsed = _coerce_optional_int(value, default)
    if parsed is None:
        parsed = default
    return max(lower, min(upper, int(parsed)))


def _coerce_float(value, default: float) -> float:
    """Strict float coercion: anything float() rejects keeps the previous value.

    API-03/ARCH-02: a SECOND `_coerce_float` — the lenient legacy-metadata parser
    now named `_coerce_legacy_metadata_float` — used to be defined further down
    this module, so Python bound the last definition and every risk/pipeline
    settings coercion above silently used the permissive one. A fat-fingered
    "1,5" became 15.0 and "20 to 40" became 30.0 where the author expected
    float() to raise and the safe stored value to survive. Keep this the
    module-wide helper; the lenient parser is only for backtest metadata.
    """
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


# ── API-02: schema + sane bounds for the capital-bearing settings sections ───
# Full editability of every gate threshold is a DELIBERATE project stance — these
# specs take NO knob away from the operator. What they add is:
#   (a) a hard refusal of numbers outside the range the enforcement code can
#       actually honour. `max_drawdown_pct` was already clamped downstream into
#       [0.01, 0.30]; `max_risk_per_trade_pct`, `max_daily_loss_pct` and the
#       `live_hard_max_*` per-order ceilings had NOTHING, so a typo persisted
#       verbatim straight into forven.exchange.risk.
#   (b) a loud 422 on payload keys no handler reads, instead of the silent drop
#       that produces the recurring "the setting does not stick" bug class.
# Only sections whose accepted keys can be enumerated exactly appear here; every
# other section keeps the previous permissive behaviour rather than risk 422-ing
# a legitimate save.
_SETTINGS_SECTION_KNOWN_KEYS: dict[str, frozenset[str]] = {
    "initial-capital": frozenset({"initial_capital"}),
    "trading-mode": frozenset({"trading_mode"}),
    "risk": frozenset({
        # per-trade / daily / drawdown limits
        "max_risk_per_trade_pct", "max_position_size_pct", "max_daily_loss_pct",
        "max_daily_loss", "max_drawdown_pct", "max_concurrent_positions",
        "paper_max_concurrent_positions", "cooldown_after_loss_hours",
        # direction books + margin mode
        "live_books_enabled", "hyperliquid_long_book_address",
        "hyperliquid_short_book_address", "hyperliquid_use_cross_margin",
        "live_equity_include_master", "live_slice_combined_books",
        # Propr challenge mirror (page group hidden unless propr is enabled)
        "propr_mirror_risk_pct",
        # liquidation proximity alerts
        "liq_distance_warn_pct", "liq_distance_critical_pct",
        # PORT-1 / SIZE-CAP-1 / BOOK-BUDGET-1 / CORR-1 live portfolio budget
        "live_portfolio_budget_enabled", "live_max_total_open_risk_pct",
        "live_max_asset_exposure_pct", "live_max_group_exposure_pct",
        "live_hard_max_per_trade_risk_pct", "live_hard_max_order_notional_pct",
        "live_max_book_notional_pct", "live_correlation_budget_enabled",
        "live_max_effective_exposure_pct", "live_correlation_window_bars",
        "live_correlation_missing_default",
        # RETRY-STORM-1 failed-open brake
        "live_failed_open_cooldown_minutes", "live_failed_open_max_attempts",
        "live_failed_open_window_hours",
        # PORT-LAYER-1 allocator
        "portfolio_layer_enabled", "portfolio_allocator_enabled",
        "portfolio_allocator_live", "portfolio_lookback_days",
        "portfolio_target_book_vol_pct", "portfolio_min_risk_multiplier",
        "portfolio_max_risk_multiplier",
        # PORT-LAYER-2 / BASKET-2 funding-carry basket
        "basket_funding_carry_enabled", "basket_rebalance_hours", "basket_n_legs",
        "basket_gross_leverage", "basket_universe_min_bars", "basket_rank_buffer",
        # LIVE-LOOP-1 paper->live graduation recommender
        "live_graduation_recommender_enabled", "graduation_min_soak_days",
        "graduation_min_paper_trades", "graduation_min_measured_trades",
        "graduation_base_arm_usd", "graduation_max_arm_usd",
        "graduation_daily_limit", "graduation_deny_cooldown_days",
        "graduation_skew_lookback_days",
        # LIQ-1 order-time liquidity guard
        "live_liquidity_guard_enabled", "live_min_daily_volume_usd",
        "live_max_spread_bps", "live_book_depth_window_bps",
        "live_max_book_participation_pct", "live_max_price_impact_bps",
        # regime gating (strict + REGIME-GATE-1 direction x regime)
        "strict_regime_gating", "regime_min_confidence",
        "allow_unknown_regime_strategies", "regime_gate_mode",
        "regime_gate_block_long", "regime_gate_block_short",
        "regime_gate_min_confidence",
        # promotion-safety gates + paper test mode
        "allow_unsupported_backtest_risk_controls", "canonical_requires_forward_proof",
        "relaxed_trade_filters_enabled", "paper_test_mode_enabled",
        "paper_test_high_activity_enabled", "paper_test_bypass_gates_enabled",
        "paper_test_local_execution_only",
        # PORT-DEDUP-1: cross-strategy paper clone-signal guard
        "paper_cross_strategy_dedup_enabled", "paper_cross_strategy_dedup_window_seconds",
    }),
}

# (minimum, maximum) inclusive. Deliberately generous — the job is to reject
# nonsense (negative risk, a 5000% per-trade ceiling, a confidence of 12), not to
# second-guess the operator inside the physically meaningful range.
_SETTINGS_SECTION_NUMERIC_BOUNDS: dict[str, dict[str, tuple[float, float]]] = {
    "initial-capital": {"initial_capital": (0.0, 1e12)},
    "risk": {
        "max_risk_per_trade_pct": (0.0001, 100.0),
        "max_position_size_pct": (0.0001, 100.0),
        "max_daily_loss_pct": (0.0001, 100.0),
        "max_daily_loss": (0.0, 1e12),
        "max_drawdown_pct": (0.0001, 100.0),
        "max_concurrent_positions": (0.0, 1000.0),
        "paper_max_concurrent_positions": (0.0, 1000.0),
        "cooldown_after_loss_hours": (0.0, 8760.0),
        "liq_distance_warn_pct": (0.0, 100.0),
        "liq_distance_critical_pct": (0.0, 100.0),
        "live_max_total_open_risk_pct": (0.0001, 100.0),
        "live_max_asset_exposure_pct": (0.0001, 10000.0),
        "live_max_group_exposure_pct": (0.0001, 10000.0),
        "live_hard_max_per_trade_risk_pct": (0.0001, 100.0),
        "live_hard_max_order_notional_pct": (0.0001, 10000.0),
        "live_max_book_notional_pct": (0.0001, 10000.0),
        "live_max_effective_exposure_pct": (0.0001, 10000.0),
        "live_correlation_window_bars": (1.0, 1e6),
        "live_correlation_missing_default": (0.0, 1.0),
        "live_failed_open_cooldown_minutes": (0.0, 10080.0),
        "live_failed_open_max_attempts": (1.0, 1000.0),
        "live_failed_open_window_hours": (0.0, 8760.0),
        "portfolio_lookback_days": (1.0, 3650.0),
        "portfolio_target_book_vol_pct": (0.0, 1000.0),
        "portfolio_min_risk_multiplier": (0.0, 100.0),
        "portfolio_max_risk_multiplier": (0.0, 100.0),
        "basket_rebalance_hours": (0.0, 8760.0),
        "basket_n_legs": (1.0, 100.0),
        "basket_gross_leverage": (0.0, 100.0),
        "basket_universe_min_bars": (0.0, 1e7),
        "basket_rank_buffer": (0.0, 100.0),
        "graduation_min_soak_days": (0.0, 3650.0),
        "graduation_min_paper_trades": (0.0, 1e5),
        "graduation_min_measured_trades": (0.0, 1e5),
        "graduation_base_arm_usd": (0.0, 1e7),
        "graduation_max_arm_usd": (0.0, 1e7),
        "graduation_daily_limit": (0.0, 1000.0),
        "graduation_deny_cooldown_days": (0.0, 3650.0),
        "graduation_skew_lookback_days": (1.0, 3650.0),
        "live_min_daily_volume_usd": (0.0, 1e15),
        "live_max_spread_bps": (0.0001, 10000.0),
        "live_book_depth_window_bps": (0.0001, 10000.0),
        "live_max_book_participation_pct": (0.0001, 100.0),
        "live_max_price_impact_bps": (0.0001, 10000.0),
        "regime_min_confidence": (0.0, 1.0),
        "regime_gate_min_confidence": (0.0, 1.0),
        # Whole percent of the member's challenge slice; the mirror caps its
        # own read at 10% (mirror_risk_fraction), the rail matches.
        "propr_mirror_risk_pct": (0.0001, 10.0),
        # PORT-DEDUP-1: 0 disables the window outright; a day is the sane ceiling
        # (beyond that it is a concurrency cap, which lives in risk.py, not here).
        "paper_cross_strategy_dedup_window_seconds": (0.0, 86400.0),
    },
}


def _validate_settings_section_payload(section: str, payload: dict) -> None:
    """Reject unknown keys and out-of-range numbers BEFORE anything is persisted.

    API-02. Raises 422 so the Settings save bar surfaces the actual refusal (it
    renders the backend detail verbatim). A key absent from the payload is
    untouched as before; an explicit ``null`` still means "keep the stored
    value", which is what the coercers already did.
    """
    known = _SETTINGS_SECTION_KNOWN_KEYS.get(section)
    if known is not None:
        unknown = sorted(str(key) for key in payload if key not in known)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"settings section '{section}' has no handler for: "
                    f"{', '.join(unknown)} — refusing the write rather than "
                    "silently dropping it"
                ),
            )

    for key, (low, high) in (_SETTINGS_SECTION_NUMERIC_BOUNDS.get(section) or {}).items():
        if key not in payload:
            continue
        raw = payload.get(key)
        if raw is None:
            continue
        # A bool here is never a legitimate limit — `float(True)` would quietly
        # become a 1% ceiling, which is exactly the class of silent misconfig
        # this guard exists to stop.
        if isinstance(raw, bool):
            raise HTTPException(
                status_code=422,
                detail=f"{section}.{key} must be a number, got a boolean",
            )
        try:
            parsed = float(raw)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422,
                detail=f"{section}.{key} must be a number, got {raw!r}",
            ) from None
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            raise HTTPException(
                status_code=422,
                detail=f"{section}.{key} must be a finite number, got {raw!r}",
            )
        if not (low <= parsed <= high):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{section}.{key} must be between {low:g} and {high:g}, got {parsed:g}"
                ),
            )


_AUDIT_IGNORE_KEYS = frozenset({
    "audit_log",
    "updated_at",
    "hyperliquid_has_key",
    "discord_bot_token_configured",
    "discord_bot_token_source",
    "discord_webhook_configured",
})


def _diff_settings_section(
    section: str,
    old_payload: dict,
    new_payload: dict,
    actor: str = "system",
) -> list[dict]:
    """Emit one audit entry per leaf that changed between old and new payloads.

    The ``section`` argument is used only as a label — entry ids are formed as
    ``f"{section}.{dot_path_from_root}"``. Volatile/derived top-level keys
    (``audit_log``, ``updated_at``, secret-presence flags, etc.) are skipped.
    """
    entries: list[dict] = []

    def walk(prefix: str, a, b):
        if isinstance(a, dict) and isinstance(b, dict):
            keys = set(a.keys()) | set(b.keys())
            for k in sorted(keys):
                if prefix == section and k in _AUDIT_IGNORE_KEYS:
                    continue
                walk(f"{prefix}.{k}", a.get(k), b.get(k))
        else:
            if a != b:
                entries.append({
                    "id": prefix,
                    "from": a,
                    "to": b,
                    "at": _now(),
                    "actor": actor,
                })

    walk(section, old_payload or {}, new_payload or {})
    return entries


def _append_settings_audit(log: list[dict], entries: list[dict], cap: int = 50) -> list[dict]:
    combined = list(log or []) + list(entries or [])
    if len(combined) > cap:
        combined = combined[-cap:]
    return combined
