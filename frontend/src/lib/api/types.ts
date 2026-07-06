/**
 * Shared API types
 */

export interface Strategy {
	name: string;
	version: string;
	description: string;
	parameters: Record<string, ParamSpec>;
	/**
	 * Optional backend identifier for API calls.
	 * Example: rule-builder strategies use "[Rule] <name>".
	 */
	api_name?: string;
}

export interface ParamSpec {
	type: string;
	default: number | string | boolean;
	min?: number;
	max?: number;
	step?: number;
	options?: string[];
}

export interface Dataset {
	symbol: string;
	timeframe: string;
	source: string;
	start_ts: string;
	end_ts: string;
	row_count: number;
	asset_class?: 'crypto' | 'stock' | 'etf' | 'forex' | 'index' | string;
	market_type?: 'crypto' | 'equity' | 'forex' | 'index' | string;
	// Venue identity stamped by the write path (forven_market parquet metadata):
	// 'perp' | 'spot' | 'unknown' | 'unstamped' (legacy pre-stamping file).
	market?: string;
	// Set when the venue could only serve a bounded recent window (e.g. Kraken's
	// most-recent-~720-candles ceiling) and the request asked for more. `warning`
	// is a human-readable explanation to show the user.
	capped?: boolean;
	warning?: string;
}

export interface Job {
	id: string;
	type: string;
	status: string;
	created_at: string;
	updated_at: string;
	error?: string;
	result_id?: string;
	progress?: string;
	strategy_id?: string;
	symbol?: string;
	timeframe?: string;
}

/** Configuration stored with a backtest result from the backend. */
export interface BacktestResultConfig {
	initial_capital?: number;
	fee_bps?: number;
	slippage_bps?: number;
	trade_mode?: 'long_only' | 'short_only' | 'both';
	position_model?: 'single_side' | 'hedged';
	allow_shorting?: boolean;
	stop_loss_pct?: number | null;
	take_profit_pct?: number | null;
	trailing_stop_pct?: number | null;
	time_stop_bars?: number | null;
	sizing_mode?: 'fraction' | 'fixed' | 'atr' | 'kelly';
	fixed_size?: number;
	risk_per_trade?: number;
	atr_stop_multiplier?: number;
	kelly_multiplier?: number;
	kelly_lookback?: number;
	leverage?: number;
	start?: string;
	end?: string;
	params?: Record<string, unknown>;
	definition_json?: Record<string, unknown>;
	warnings?: string[];
	status?: string;
	error?: string | null;
	/** Present on optimization results -- ID of auto-run best-params backtest */
	best_backtest_id?: string;
	/** Any extra fields the backend may include */
	[key: string]: unknown;
}

/** Full configuration object used by the Simulation workspace frontend. */
export interface SimulationConfig {
	strategy: string;
	symbol: string;
	timeframe: string;
	start_date: string;
	end_date: string;
	params: Record<string, unknown>;
	capital: number;
	fees: number;
	slippage: number;
	trade_mode?: 'long_only' | 'short_only' | 'both';
	allow_shorting: boolean;
	stop_loss_pct: number | null;
	take_profit_pct: number | null;
	trailing_stop_pct: number | null;
	time_stop_bars: number | null;
	sizing_mode: 'fraction' | 'fixed' | 'atr' | 'kelly';
	fixed_size: number;
	risk_per_trade: number;
	atr_stop_multiplier: number;
	kelly_multiplier: number;
	kelly_lookback: number;
	leverage: number;
	optimization: OptimizationConfig;
	walkForward: WalkForwardConfig;
	robustness: RobustnessConfig;
}

export interface OptimizationConfig {
	objective: string;
	n_trials: number;
}

export interface WalkForwardConfig {
	cv_method: string;
	n_splits: number;
	train_ratio: number;
	purge_gap: number;
	embargo_pct: number;
	objective: string;
	n_trials: number;
}

export interface RobustnessConfig {
	monte_carlo_simulations: number;
}

export interface BacktestResult {
	id: string;
	result_id?: string;
	job_id: string;
	strategy_name: string;
	strategy_id?: string | null;
	lifecycle_strategy_id?: string | null;
	strategy_version: string;
	symbol: string;
	timeframe: string;
	created_at: string;
	metrics: BacktestMetrics;
	config: BacktestResultConfig;
	equity_curve?: EquityPoint[];
	benchmark_curve?: EquityPoint[];
	/** Full-window (in-sample + out-of-sample) curves for the entire-timeframe chart.
	 *  Absent on results created before this was added → fall back to the OOS curves. */
	equity_curve_full?: EquityPoint[];
	benchmark_curve_full?: EquityPoint[];
	trades?: Trade[];
	result_type?: string;
	status?: string;
	error?: string | null;
	verdict?: string;
	verdict_reasons?: string[];
	description?: string;
}

export interface BacktestMetrics {
	// Core metrics
	total_return: number;
	cagr?: number;
	monthly_return_pct?: number;
	annualized_return_pct?: number;
	backtest_days?: number;
	backtest_months?: number;
	max_drawdown: number;
	sharpe_ratio: number;
	sortino_ratio?: number;
	win_rate: number;
	profit_factor?: number;
	total_trades: number;

	// Extended metrics
	calmar_ratio?: number;
	omega_ratio?: number;
	tail_ratio?: number;
	value_at_risk?: number;
	expected_shortfall?: number;
	beta?: number;
	alpha?: number;

	// Drawdown metrics
	max_drawdown_duration?: number;
	avg_drawdown_duration?: number;

	// Trade-level metrics
	avg_mae?: number;
	avg_mfe?: number;
	edge_ratio?: number;
	avg_trade_duration?: number;
	expectancy?: number;
	recovery_factor?: number;

	// Optimization-specific (present when result_type === 'optimization')
	best_params?: Record<string, unknown>;
	best_value?: number;
	objective?: string;
	n_trials?: number;
	trials_summary?: TrialSummary[];

	// Walk-forward specific (present when result_type === 'walk_forward')
	most_robust_params?: Record<string, unknown>;
	avg_train_metric?: number;
	avg_test_metric?: number;
	overfitting_ratio?: number;
	n_folds?: WalkForwardFoldSummary[];

	// Allow additional backend-specific fields
	[key: string]: unknown;
}

export interface TrialSummary {
	number: number;
	value: number | null;
	params: Record<string, unknown>;
	state?: string;
}

export interface WalkForwardFoldSummary {
	fold: number;
	train_start: string;
	train_end: string;
	test_start: string;
	test_end: string;
	train_metric: number;
	test_metric: number;
	best_params: Record<string, unknown>;
}

export interface RegimePerformance {
	total_return_pct?: number;
	sharpe_ratio?: number;
	max_drawdown_pct?: number;
	pct_of_total?: number;
	n_periods?: number;
}

export interface EquityPoint {
	timestamp: string;
	equity: number;
}

export interface Trade {
	entry_time: string;
	entry_price: number;
	exit_time: string;
	exit_price: number;
	size: number;
	pnl: number;
	return_pct: number;
	mae?: number;  // Max adverse excursion (%)
	mfe?: number;  // Max favorable excursion (%)
	direction?: string;  // 'long' or 'short'
	bars_held?: number;  // Duration in bars
}

// ---------------------------------------------------------------------------
// Health Monitor types
// ---------------------------------------------------------------------------

export type HealthState = 'green' | 'amber' | 'red';

export interface ComponentStatus {
	name: string;
	state: HealthState;
	last_seen: string | null;
	message: string;
	component_type: string;
}

export interface HealthDataCheck {
	name: string;
	passed: boolean;
	severity: string;
	detail: string;
}

export interface HealthAlert {
	severity: string;
	component: string;
	message: string;
	timestamp: string;
	action_taken: string;
	dedupe_key: string;
}

export interface HealthStatusResponse {
	components: ComponentStatus[];
	data_checks: HealthDataCheck[];
	overall: HealthState;
	checked_at: string | null;
	monitor_running: boolean;
}

export interface HealthAlertsResponse {
	alerts: HealthAlert[];
	count: number;
}

export interface StrategyManagerRow {
	id: string;
	name?: string;
	symbol?: string;
	timeframe?: string;
	stage?: string;
	status?: string;
	has_backtest_results?: boolean;
	recovery_active?: boolean;
	recovery_status?: string | null;
	recovery_attempt_count?: number;
	recovery_last_error?: string | null;
	recovery_cooldown_until?: string | null;
	created_at?: string;
	[key: string]: unknown;
}

/**
 * Simple concurrency limiter to prevent browser connection exhaustion.
 * Browsers allow ~6 concurrent connections per origin; we cap at 50
 * to leave headroom for user-initiated requests and allow HTTP/2 multiplexing.
 */
