// Allowed ranges for the Settings page's number fields.
//
// `enforced` ranges mirror a bound the backend applies on save: the risk rails in
// forven/settings_apply.py (_SETTINGS_SECTION_NUMERIC_BOUNDS, which refuses an
// out-of-range write) and the clamps in forven/api_core.py, the data collector
// and forven/throughput_policy.py (which silently pull a value back into range).
// tests/test_settings_bounds.py pins the risk rails to the backend table.
// Everything else is the field's sensible range (no negative counts, fractions in
// 0-1), shown as a hint and set on the input; the backend does not police it.
//
// step: 1 for whole numbers (counts, days, bars), 'any' for decimals.

export interface NumberBounds {
  min?: number;
  max?: number;
  step: number | 'any';
  enforced?: boolean;
}

type Spec = [min: number | null, max: number | null, step: number | 'any', enforced?: 'enforced'];

const E = 'enforced' as const;

const SPECS: Record<string, Spec> = {
  // ---- trading: capital & risk (settings_apply risk rails) ----
  'initial-capital.initial_capital': [0, 1e12, 'any', E],
  'risk.max_risk_per_trade_pct': [0.0001, 100, 'any', E],
  'risk.max_concurrent_positions': [0, 1000, 1, E],
  'risk.paper_max_concurrent_positions': [0, 1000, 1, E],
  'risk.live_max_leverage': [1, 3, 'any', E],
  'risk.liq_distance_warn_pct': [0, 100, 'any', E],
  'risk.liq_distance_critical_pct': [0, 100, 'any', E],
  'risk.max_daily_loss_pct': [0.0001, 100, 'any', E],
  'risk.max_drawdown_pct': [0.0001, 100, 'any', E],
  'risk.cooldown_after_loss_hours': [0, 8760, 'any', E],
  'risk.live_max_total_open_risk_pct': [0.0001, 100, 'any', E],
  'risk.live_max_asset_exposure_pct': [0.0001, 10000, 'any', E],
  'risk.live_max_group_exposure_pct': [0.0001, 10000, 'any', E],
  'risk.portfolio_lookback_days': [1, 3650, 1, E],
  'risk.portfolio_target_book_vol_pct': [0, 1000, 'any', E],
  'risk.portfolio_min_risk_multiplier': [0, 100, 'any', E],
  'risk.portfolio_max_risk_multiplier': [0, 100, 'any', E],
  'risk.basket_rebalance_hours': [0, 8760, 'any', E],
  'risk.basket_n_legs': [1, 100, 1, E],
  'risk.basket_gross_leverage': [0, 100, 'any', E],
  'risk.basket_rank_buffer': [0, 100, 1, E],
  'risk.basket_universe_min_bars': [0, 1e7, 1, E],
  'risk.live_failed_open_cooldown_minutes': [0, 10080, 'any', E],
  'risk.live_failed_open_max_attempts': [1, 1000, 1, E],
  'risk.live_failed_open_window_hours': [0, 8760, 'any', E],
  'risk.live_max_effective_exposure_pct': [0.0001, 10000, 'any', E],
  'risk.live_correlation_window_bars': [1, 1e6, 1, E],
  'risk.live_correlation_missing_default': [0, 1, 'any', E],
  'risk.live_hard_max_per_trade_risk_pct': [0.0001, 100, 'any', E],
  'risk.live_hard_max_order_notional_pct': [0.0001, 10000, 'any', E],
  'risk.live_max_book_margin_pct': [0.0001, 100, 'any', E],
  'risk.live_min_daily_volume_usd': [0, 1e15, 'any', E],
  'risk.live_max_spread_bps': [0.0001, 10000, 'any', E],
  'risk.live_book_depth_window_bps': [0.0001, 10000, 'any', E],
  'risk.live_max_book_participation_pct': [0.0001, 100, 'any', E],
  'risk.live_max_price_impact_bps': [0.0001, 10000, 'any', E],
  'risk.regime_min_confidence': [0, 1, 'any', E],
  'risk.regime_gate_min_confidence': [0, 1, 'any', E],
  'risk.propr_mirror_risk_pct': [0.0001, 10, 'any', E],
  'risk.graduation_min_soak_days': [0, 3650, 'any', E],
  'risk.graduation_min_paper_trades': [0, 1e5, 1, E],
  'risk.graduation_min_measured_trades': [0, 1e5, 1, E],
  'risk.graduation_base_arm_usd': [0, 1e7, 'any', E],
  'risk.graduation_max_arm_usd': [0, 1e7, 'any', E],
  'risk.graduation_daily_limit': [0, 1000, 1, E],
  'risk.graduation_deny_cooldown_days': [0, 3650, 'any', E],
  'risk.graduation_skew_lookback_days': [1, 3650, 'any', E],

  // ---- lab: quick screen ----
  'pipeline.quick_screen.min_total_return_pct': [null, null, 'any'],
  'pipeline.quick_screen.max_drawdown_pct': [0, 100, 'any'],
  'pipeline.quick_screen.min_sharpe': [null, null, 'any'],
  'pipeline.quick_screen.min_profit_factor': [0, null, 'any'],
  'pipeline.quick_screen.min_trades': [0, null, 1],
  'pipeline.quick_screen.min_robustness_score': [0, 100, 'any'],
  'pipeline.quick_screen.min_is_sharpe': [null, null, 'any'],
  'pipeline.quick_screen.fitness_min_trades': [0, null, 1],
  'pipeline.quick_screen.fitness_min_profit_factor': [0, null, 'any'],

  // ---- lab: gauntlet ----
  'pipeline.gauntlet_step_stale_minutes': [1, null, 1],
  'pipeline.gauntlet.min_robustness_score': [0, 100, 'any'],
  'pipeline.gauntlet.min_trades': [0, null, 1],
  'pipeline.gauntlet.min_sharpe': [null, null, 'any'],
  'pipeline.gauntlet.hard_min_is_sharpe': [null, null, 'any'],
  'pipeline.gauntlet.min_oos_profit_factor': [0, null, 'any'],
  'pipeline.gauntlet.max_drawdown_pct': [0, 100, 'any'],
  'pipeline.gauntlet.mc_max_dd_p95': [0, 100, 'any'],
  'pipeline.gauntlet.wfa_min_folds': [1, null, 1],
  'pipeline.gauntlet.wfa_baseline_min_alpha_pct': [null, null, 'any'],
  'pipeline.gauntlet.wfa_baseline_min_alpha_t': [null, null, 'any'],
  'pipeline.gauntlet.wfa_baseline_min_oos_days': [0, null, 1],
  'pipeline.gauntlet.async_result_max_age_minutes': [1, null, 1],
  'pipeline.robustness_thresholds.wfa_fold_pass_rate_min': [0, 100, 'any'],
  'pipeline.robustness_thresholds.param_jitter_pass_rate_min': [0, 100, 'any'],
  'pipeline.robustness_thresholds.min_deflated_sharpe': [0, 1, 'any'],
  'pipeline.robustness_thresholds.dsr_swarm_lookback_days': [1, null, 1],
  'pipeline.robustness_thresholds.param_jitter_max_iterations': [1, null, 1],
  'pipeline.robustness_thresholds.param_jitter_max_bars': [1, null, 1],
  'pipeline.robustness_thresholds.param_jitter_deadline_seconds': [1, null, 1],
  'pipeline.robustness_thresholds.cost_stress_max_degradation_pct': [0, 100, 'any'],
  'pipeline.robustness_thresholds.wfa_min_fold_trades': [0, null, 1],

  // ---- lab: paper and live gates ----
  'pipeline.dethrone.paper_min_soak_days': [0, null, 'any'],
  'pipeline.dethrone.paper_max_soak_days': [0, null, 'any'],
  'pipeline.dethrone.paper_min_closed_trades': [0, null, 1],
  'pipeline.paper_trading.min_paper_days': [0, null, 'any'],
  'pipeline.paper_trading.min_closed_trades': [0, null, 1],
  'pipeline.paper_trading.min_total_return_pct': [null, null, 'any'],
  'pipeline.paper_trading.max_drawdown_pct': [0, 100, 'any'],
  'pipeline.paper_trading.min_profit_factor_live': [0, null, 'any'],
  'pipeline.paper_trading.min_paper_sharpe': [null, null, 'any'],
  'pipeline.paper_trading.min_profit_factor_paper': [0, null, 'any'],
  'pipeline.paper_trading.pf_position_reduction_threshold': [0, null, 'any'],
  'pipeline.paper_trading.max_oos_is_ratio': [0, null, 'any'],

  // ---- lab: safety floors (fractions 0-1 where the description says so) ----
  'pipeline.safety_floors.min_trades': [0, null, 1],
  'pipeline.safety_floors.min_robustness_score': [0, 100, 'any'],
  'pipeline.safety_floors.mc_max_dd_p95': [0, 1, 'any'],
  'pipeline.safety_floors.wfa_fold_pass_rate_min': [0, 1, 'any'],
  'pipeline.safety_floors.wfa_min_folds': [1, null, 1],
  'pipeline.safety_floors.param_jitter_pass_rate_min': [0, 1, 'any'],
  'pipeline.safety_floors.live_min_closed_trades': [0, null, 1],
  'pipeline.safety_floors.live_max_drawdown_pct': [0, 1, 'any'],

  // ---- lab: capacity, live graduated, graduation ----
  'pipeline.paper_wip_cap': [0, null, 1],
  'pipeline.graveyard_strategy_limit': [0, null, 1],
  'pipeline.live_graduated.decay_kill_switch_pct': [0, 100, 'any'],

  // ---- lab: backtest windows and costs (0 = inherit the default window) ----
  'backtesting-defaults.backtest_duration_days': [1, null, 1],
  'backtesting-defaults.quick_screen_duration_days': [0, null, 1],
  'backtesting-defaults.timeframe_sweep_duration_days': [0, null, 1],
  'backtesting-defaults.optimization_duration_days': [0, null, 1],
  'backtesting-defaults.confirmation_duration_days': [0, null, 1],
  'backtesting-defaults.walk_forward_duration_days': [0, null, 1],
  'backtesting-defaults.cost_stress_duration_days': [0, null, 1],
  'backtesting-defaults.evolution_duration_days': [0, null, 1],
  'backtesting-defaults.rolling_backtest_days': [1, null, 1],
  'backtesting-defaults.backtest_fee_bps': [0, null, 'any'],
  'backtesting-defaults.backtest_slippage_bps': [0, null, 'any'],
  'backtesting-defaults.walkforward_folds': [1, null, 1],
  'backtesting-defaults.walkforward_train_ratio': [0, 1, 'any'],

  // ---- system: bot operations (api_core clamps) ----
  'bot-operations.pipeline_target_clear_hours': [1, 168, 1, E],
  'bot-operations.ideation_interval_minutes': [1, 1440, 1, E],
  'bot-operations.coding_interval_minutes': [1, 1440, 1, E],
  'bot-operations.testing_interval_minutes': [1, 1440, 1, E],
  'bot-operations.graduation_interval_minutes': [1, 10080, 1, E],
  'bot-operations.scanner_signal_interval_minutes': [1, 1440, 1, E],
  'bot-operations.scanner_execution_interval_minutes': [1, 1440, 1, E],
  'bot-operations.daemon_candle_cache_refresh_seconds': [15, 3600, 1, E],
  'bot-operations.pipeline_assignments_per_cycle': [1, 20, 1, E],
  'bot-operations.pipeline_drain_max_seconds': [30, 1800, 1, E],
  'bot-operations.pipeline_gate_failure_archive_attempts': [1, 10, 1, E],
  'bot-operations.backtest_matrix_workers': [1, 8, 1, E],
  'bot-operations.backtest_subprocess_budget': [1, 8, 1, E],
  'bot-operations.gauntlet_drain_workers': [1, 8, 1, E],
  'bot-operations.pipeline_saturation_threshold': [10, 500, 1, E],
  'bot-operations.pipeline_resume_threshold': [5, 400, 1, E],
  'bot-operations.agent_task_claim_limit': [1, 20, 1, E],
  'bot-operations.brain_task_claim_limit': [1, 20, 1, E],
  'bot-operations.task_stale_recovery_minutes': [1, 1440, 1, E],
  'bot-operations.brain_queue_max_pending': [1, null, 1],
  'research.strategy_creation_daily_budget': [0, 500, 1, E],
  'research.strategy_creation_max_in_flight': [1, null, 1],
  'agents.assistant_max_tool_rounds': [2, 40, 1, E],

  // ---- data ----
  'data-engine.source_reconciliation_max_divergence_pct': [0, 100, 'any'],
  'data-sla.live_missed_bars': [0, null, 1],
  'data-sla.live_floor_minutes': [0, null, 1],
  'data-sla.paper_missed_bars': [0, null, 1],
  'data-sla.paper_floor_minutes': [0, null, 1],
  'data-sla.pipeline_missed_bars': [0, null, 1],
  'data-sla.pipeline_floor_minutes': [0, null, 1],
  'data-sla.universe_missed_bars': [0, null, 1],
  'data-sla.universe_floor_minutes': [0, null, 1],
  'data-sla.idle_missed_bars': [0, null, 1],
  'data-sla.idle_floor_minutes': [0, null, 1],
  'data-sla.breach_multiplier': [1, null, 'any'],
  'data-collector.tick_seconds': [30, 3600, 1, E],
  'data-collector.max_tick_seconds': [5, 3600, 1, E],
  'data-collector.max_requests_per_minute': [1, 100000, 1, E],
  'data-collector.strike_out_after': [1, 100, 1, E],
  'data-storage.trash_retention_days': [0, null, 1],
  'data-storage.min_free_disk_gb': [0, null, 'any'],
  'data-storage.revision_keep_days': [0, null, 1],
  'data-universe.size': [1, null, 1],
  'data-universe.intraday_top': [0, null, 1],
  'data-universe.minute_top': [0, null, 1],
  'data-universe.metrics_days': [0, null, 1],
  'pipeline.retention_backtest_trash_days': [0, null, 1],
  'pipeline.retention_activity_log_days': [0, null, 1],
  'pipeline.retention_scanner_results_days': [0, null, 1],
  'pipeline.retention_gate_rejections_days': [0, null, 1],
};

/** Range and step for a number field; `undefined` when the field has no entry. */
export function numberBounds(id: string): NumberBounds | undefined {
  const spec = SPECS[id];
  if (!spec) return undefined;
  const [min, max, step, enforced] = spec;
  return {
    ...(min !== null ? { min } : {}),
    ...(max !== null ? { max } : {}),
    step,
    ...(enforced ? { enforced: true } : {}),
  };
}

export function boundedFieldIds(): string[] {
  return Object.keys(SPECS);
}

function fmt(n: number): string {
  return Math.abs(n) >= 1e6 ? n.toExponential(0).replace('+', '') : n.toLocaleString('en-US', { maximumFractionDigits: 4 });
}

/** "0 to 100", "at least 1", "up to 3"; '' when unbounded. */
export function describeRange(bounds: NumberBounds | undefined): string {
  if (!bounds) return '';
  const { min, max } = bounds;
  if (min !== undefined && max !== undefined) return `${fmt(min)} to ${fmt(max)}`;
  if (min !== undefined) return `at least ${fmt(min)}`;
  if (max !== undefined) return `up to ${fmt(max)}`;
  return '';
}

/** True when `value` is a number outside the field's range. */
export function outOfRange(value: unknown, bounds: NumberBounds | undefined): boolean {
  if (!bounds || value === null || value === undefined || value === '') return false;
  const n = Number(value);
  if (Number.isNaN(n)) return false;
  return (bounds.min !== undefined && n < bounds.min) || (bounds.max !== undefined && n > bounds.max);
}
