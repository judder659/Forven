import {
	ACTIVE_API_BASE,
	API_BASE,
	fetchApi,
} from './core';
import type { TaskAuditEvent, TaskContainer } from './lifecycle';
import type { PendingSignal, SessionIndicatorsResponse, TradeMarkersResponse } from './paper';

export type ForvenProvider =
	| 'openai'
	| 'minimax'
	| 'lmstudio'
	| 'zai'
	| 'openrouter'
	| 'anthropic'
	| 'deepseek'
	| 'groq'
	| 'gemini'
	| 'cerebras'
	| 'mistral'
	| 'xai'
	| 'together'
	| 'opencode-zen'
	| 'opencode-go';

// ============== Forven Classic Compatibility ==============

export interface ForvenDashboardResponse {
	execution_mode?: 'paper' | 'live' | string;
	trading_allowed?: boolean;
	trading_reason?: string;
	paused?: boolean;
	paused_at?: string | null;
	generation_paused?: boolean;
	generation_paused_at?: string | null;
	recovery?: {
		active?: boolean;
		status?: string;
		started_at?: string | null;
		position_count?: number;
		discrepancy_count?: number;
		requires_operator?: boolean;
		batch_id?: string | null;
		summary?: string;
		open_order_count?: number;
		last_checked_at?: string | null;
		network?: string | null;
	};
	account?: {
		accountValue?: number;
		totalMarginUsed?: number;
		withdrawable?: number;
		network?: string | null;
		source?: string | null;
		synced_at?: string | null;
	};
	prices?: Record<string, number>;
	scan_count?: number;
	daemon_running?: boolean;
	started_at?: string | null;
	last_scan?: string | null;
	risk?: {
		kill_switch_active?: boolean;
		daily_loss_halt?: boolean;
		high_water_mark?: number;
		drawdown_pct?: number;
	};
	daily_risk?: {
		start_equity?: number;
		current_equity?: number;
		pnl_pct?: number;
		loss_pct?: number;
	};
	circuit_breakers?: {
		hl_price?: string;
		hl_trade?: string;
		hl_account?: string;
	};
	sentiment?: Record<string, unknown>;
	simulation_active?: boolean;
	simulation_phase?: string;
	simulation_time?: string;
	simulation_progress?: number;
	simulation_prices?: Record<string, number>;
}

export interface RegimeGateStance {
	asset: string;
	regime?: string | null;
	confidence?: number | null;
	since?: number | null;
	restricted: string[];
}

export interface RegimeGateEvent {
	id: number;
	ts: string;
	strategy_id: string;
	asset: string;
	direction: string;
	regime: string;
	confidence?: number | null;
	mode: string;
	decision: string;
	execution_type?: string | null;
	ref_price?: number | null;
	mtm_pct?: number | null;
	mtm_evaluated_at?: string | null;
}

export interface RegimeGateStatus {
	mode: 'off' | 'observe' | 'enforce';
	block_long: string[];
	block_short: string[];
	min_confidence: number;
	mtm_horizon_hours: number;
	stances: RegimeGateStance[];
	events: RegimeGateEvent[];
	aggregates: {
		window_days: number;
		events: number;
		mtm_n: number;
		mtm_avg_pct: number | null;
		by_execution?: Record<string, { events: number; mtm_n: number; mtm_avg_pct: number | null }>;
	};
}

export interface ForvenRiskStatus {
	system_paused?: boolean;
	kill_switch_enabled?: boolean;
	kill_switch_active?: boolean;
	kill_switch_triggered_at?: string | null;
	daily_loss_halt?: boolean;
	high_water_mark?: number;
	daily_start_equity?: number;
	open_positions?: number;
	/** Largest single-trade risk fraction across open positions (display-only). */
	current_per_trade_risk?: number;
	recovery_active?: boolean;
	recovery_status?: string;
	recovery_summary?: string;
	/** REGIME-GATE-1: direction×regime entry gate status (null on gate errors). */
	regime_gate?: RegimeGateStatus | null;
	limits?: {
		max_drawdown?: number;
		daily_loss_limit?: number;
		max_risk_per_trade?: number;
		portfolio_budget?: number;
		[key: string]: number | undefined;
	};
	portfolio?: {
		total_net_risk?: number;
		groups?: Record<string, {
			gross_long?: number;
			gross_short?: number;
			net?: number;
		}>;
	};
	/** Paper-sandbox complement of `portfolio` (display-only, never gating). */
	portfolio_paper?: {
		total_net_risk?: number;
		groups?: Record<string, {
			gross_long?: number;
			gross_short?: number;
			net?: number;
		}>;
	};
	open_positions_paper?: number;
	current_per_trade_risk_paper?: number;
	/** PORT-1: the LIVE account-level budget (dollar risk-to-stop + net exposure vs equity). */
	portfolio_budget_live?: {
		enabled?: boolean;
		equity_usd?: number | null;
		equity_available?: boolean;
		limits_pct?: Record<string, number>;
		total_open_risk_usd?: number;
		total_open_risk_limit_usd?: number | null;
		total_open_risk_used_frac?: number | null;
		stops_missing?: number;
		per_asset?: Record<string, {
			net_notional_usd?: number;
			risk_usd?: number;
			positions?: number;
			group?: string;
			limit_usd?: number | null;
		}>;
		per_group?: Record<string, {
			net_notional_usd?: number;
			risk_usd?: number;
			positions?: number;
			limit_usd?: number | null;
		}>;
		/** BOOK-BUDGET-1: per-wallet (direction book) capacity and usage. */
		per_book?: Record<string, {
			gross_notional_usd?: number;
			risk_usd?: number;
			positions?: number;
			equity_usd?: number | null;
			limit_usd?: number | null;
		}>;
		positions?: Array<{
			trade_id?: string;
			asset?: string;
			direction?: string;
			strategy_id?: string;
			notional_usd?: number;
			risk_usd?: number;
			stop_price?: number | null;
			group?: string;
		}>;
		groups?: Record<string, string[]>;
		/** GO-LIVE-1: per-strategy notional ceilings accepted at go-live. */
		strategy_ceilings?: Record<string, {
			ceiling_usd?: number;
			asset?: string | null;
			set_by?: string;
			set_at?: string;
			/** current stage of the owning strategy ('bot' for Bot Factory keys) */
			stage?: string;
		}>;
		/** Live-stage strategies with no go-live ceiling recorded (pre-existing lives). */
		ceilings_missing?: string[];
	};
	/** LIQ-1: order-time liquidity guard (limits + recent admit/block decisions). */
	liquidity_guard_live?: {
		enabled?: boolean | null;
		error?: string;
		limits?: Record<string, number>;
		recent_decisions?: Array<{
			asset?: string;
			side?: string;
			allowed?: boolean;
			reason?: string;
			checked_at?: number;
			order_notional_usd?: number;
			day_volume_usd?: number;
			spread_bps?: number;
			near_depth_usd?: number;
			est_impact_bps?: number;
		}>;
	};
}

export interface ForvenRegimeSnapshot {
	[asset: string]: {
		regime?: string;
		confidence?: number;
		adx?: number;
		ema_alignment?: string;
		atr_ratio?: number;
		rsi?: number;
		/** epoch seconds of the last regime flip (null = not yet observed) */
		since?: number | null;
		asset?: string;
	};
}

export interface RegimeSeriesSegment {
	start: string;
	end: string;
	regime: string;
}

export interface RegimeSeriesResponse {
	symbol: string;
	series?: string | null;
	timeframe: string;
	bars?: number;
	segments: RegimeSeriesSegment[];
}

export async function getRegimeSeries(
	symbol: string,
	timeframe = '1h',
	bars = 1000
): Promise<RegimeSeriesResponse> {
	const params = new URLSearchParams({ symbol, timeframe, bars: String(bars) });
	return fetchApi<RegimeSeriesResponse>(`/regime/series?${params.toString()}`);
}

export interface ForvenEquityPoint {
	time: string;
	value: number;
	pnl: number;
	is_current?: boolean;
}

export interface ForvenEquityHistory {
	base: number;
	curve: ForvenEquityPoint[];
}

export interface ForvenSentimentSnapshot {
	composite?: number;
	[key: string]: unknown;
}

export interface ForvenTrade {
	id?: string;
	display_id?: string;
	asset?: string;
	symbol?: string;
	direction?: string;
	strategy?: string;
	strategy_id?: string;
	strategy_name?: string;
	source?: string;
	execution_type?: string;
	entry_price?: number;
	signal_entry_price?: number | null;
	fill_entry_price?: number | null;
	exit_price?: number | null;
	signal_exit_price?: number | null;
	fill_exit_price?: number | null;
	entry_slippage_bps?: number | null;
	exit_slippage_bps?: number | null;
	size?: number;
	risk_pct?: number | null;
	leverage?: number;
	pnl?: number | null;
	pnl_pct?: number | null;
	pnl_usd?: number | null;
	net_pnl_pct?: number | null;
	fees_pct?: number | null;
	book?: string | null;
	status?: string;
	timeframe?: string | null;
	/** market regime at entry (causal classifier label); null on pre-stamp rows */
	regime?: string | null;
	opened_at?: string | null;
	closed_at?: string | null;
	created_at?: string | null;
	signal_data?: unknown;
}

export interface ForvenTradesPage {
	trades: ForvenTrade[];
	total: number;
	limit: number;
	offset: number;
	status: string | null;
	sort?: string;
	sort_dir?: string;
}

/** Aggregate blotter stats over the full filtered ledger (see /api/trades/stats).
 * Realized metrics are dollar-based (pnl_usd over CLOSED trades). win_rate /
 * profit_factor / avg_win / avg_loss / expectancy are null when undefined. */
export interface ForvenTradesStats {
	total: number;
	open_count: number;
	closed_count: number;
	failed_count: number;
	open_exposure: number;
	net_pnl: number;
	gross_profit: number;
	gross_loss: number;
	wins: number;
	losses: number;
	decided: number;
	win_rate: number | null;
	profit_factor: number | null;
	avg_win: number | null;
	avg_loss: number | null;
	expectancy: number | null;
	best: number | null;
	worst: number | null;
}

export interface ForvenTradesQuery {
	status?: string;
	limit?: number;
	offset?: number;
	asset?: string;
	strategy?: string;
	direction?: string;
	execution_type?: string;
	opened_from?: string;
	opened_to?: string;
	search?: string;
	sort?: string;
	sort_dir?: 'asc' | 'desc';
}

export interface ForvenStrategyPerformance {
	strategy?: string;
	total_trades?: number;
	wins?: number;
	losses?: number;
	avg_pnl?: number | null;
	total_pnl_usd?: number | null;
	best_trade?: number | null;
	worst_trade?: number | null;
	open_count?: number;
}

export interface ForvenAgentTask {
	id?: string | number;
	agent_id?: string;
	type?: string;
	title?: string;
	description?: string;
	status?: string;
	priority?: number;
	created_at?: string | null;
	started_at?: string | null;
	completed_at?: string | null;
	input_data?: unknown;
	output_data?: unknown;
	error?: string | null;
	source?: string;
}

export interface QueueProcessingOptions {
	process_agent_tasks?: boolean;
	process_brain_tasks?: boolean;
	recover_stale?: boolean;
	stale_minutes?: number;
	fail_agents?: string[];
	ignore_bot_singleton_guard?: boolean;
}

export interface BotSingletonLockStatus {
	singleton_supported?: boolean;
	singleton_enforced?: boolean;
	lock_path?: string;
	current_pid?: number;
	held_by_current_process?: boolean;
	lock_held?: boolean;
	active_pid?: number | null;
	active_pid_running?: boolean;
	other_process_active?: boolean;
	stale_pid?: number | null;
}

export interface QueueProcessingResult {
	ok: boolean;
	recovered: {
		agent_requeued: number;
		agent_failed: number;
		brain_requeued: number;
	};
	agent_tasks_processed: boolean;
	brain_tasks_processed: boolean;
	processing_requested?: boolean;
	delegated_to_bot?: boolean;
	queue_request_id?: string | null;
	queue_request_status?: string | null;
	stale_recovery_enabled: boolean;
	stale_minutes: number;
	bot_available?: boolean;
	bot_error?: string | null;
	guard_blocked?: boolean;
	guard_reason?: string | null;
	bot_lock?: BotSingletonLockStatus;
}

export interface ForvenSchedulerJob {
	id?: string | number;
	name?: string;
	schedule_type?: string;
	schedule_expr?: string;
	next_run_at?: string | null;
	last_run_at?: string | null;
	last_error?: string | null;
	timezone?: string | null;
	command?: string | null;
	payload?: string | Record<string, unknown> | null;
	last_status?: string | null;
	enabled?: boolean;
}

export interface ForvenScannerState {
	strategies?: unknown[];
	[key: string]: unknown;
}

export interface ForvenAgent {
	id?: string;
	name?: string;
	role?: string;
	model?: string;
	model_id?: string | null;
	visibility?: 'visible' | 'internal' | string;
	status?: string;
	enabled?: boolean;
	schedule_type?: string | null;
	schedule_expr?: string | null;
	instructions?: string | null;
	has_discord_token?: boolean;
	created_at?: string; updated_at?: string;
	[key: string]: unknown;
}

export interface ForvenAgentDocumentsResponse {
	soul: string;
	agents: string;
	role: string;
}

export interface ForvenAgentUpdatePayload {
	name?: string;
	role?: string;
	model?: string;
	model_id?: string | null;
	schedule_type?: string;
	schedule_expr?: string;
	enabled?: boolean;
	visibility?: 'visible' | 'internal';
	instructions?: string;
	discord_token?: string;
}
export interface ForvenAgentDocumentBody {
	content: string;
}

export interface ForvenAgentDiscordTestResponse {
	status: string;
	agent_id: string;
	agent_name?: string;
	channel: string;
	channel_id: string;
	tested_at: string;
}

export interface ForvenAgentModelOption {
	key: string;
	provider: ForvenProvider;
	model_id: string;
	label: string;
	enabled: boolean;
}

export interface ForvenAgentModelOptionsResponse {
	options?: ForvenAgentModelOption[];
	providers?: Array<{
		provider: string;
		default_model_id?: string;
		model_count?: number;
		source?: string;
		error?: string | null;
	}>;
	generated_at?: string;
}

export interface ForvenAuthProviderStatus {
	provider: ForvenProvider;
	configured: boolean;
	/**
	 * Whether the operator has explicitly connected this provider (saved a key or
	 * completed OAuth) and thereby authorized spend against it. Distinct from
	 * `configured`, which can be true merely because an env credential exists.
	 * Backend (`GET /api/auth/providers`) is being updated to emit this; when the
	 * field is absent, callers fall back to `configured && status === 'active'`.
	 */
	connected?: boolean;
	status:
		| 'active'
		| 'expiring_soon'
		| 'expired'
		| 'not_configured'
		| 'error'
		| 'invalid'
		| 'needs_reauth';
	expires_at?: string | null;
	expires_in?: string | null;
	has_refresh_token: boolean;
	login_command: string;
	refresh_command: string;
	supports_oauth?: boolean;
	requires_token?: boolean;
	base_url?: string | null;
	last_refresh_error?: string | null;
	last_refresh_at?: string | null;
}

export interface ForvenAuthProviderProfilePayload {
	access_token?: string;
	api_key?: string;
	refresh_token?: string;
	expires_at?: string;
	expires_in?: number;
	base_url?: string;
}

export interface ForvenAuthProviderOAuthStartResponse {
	provider: ForvenProvider;
	flow: 'authorization_code' | 'device_code';
	state: string;
	authorize_url?: string;
	code_verifier?: string;
	verification_url?: string;
	user_code?: string;
	interval?: number;
	auto_callback?: boolean;
	bind_error?: string;
}

export type ForvenAuthProviderOAuthStatus =
	| { status: 'awaiting_user' }
	| { status: 'code_received' }
	| { status: 'complete' }
	| { status: 'expired' }
	| { status: 'denied' }
	| { status: 'slow_down'; interval: number }
	| { status: 'error'; error?: string };

export interface ForvenAuthProviderOAuthCompleteBody {
	code?: string;
	state: string;
	code_verifier?: string;
}

export interface ForvenAuthProviderOAuthCompleteResponse {
	ok: boolean;
	provider: ForvenProvider;
	status: 'active' | 'not_configured' | 'expiring_soon' | 'expired';
	message?: string;
}

export interface ForvenAuthProvidersResponse {
	providers: ForvenAuthProviderStatus[];
	configure_command: string;
	status_command: string;
	auth_file: string;
}

export interface ForvenModelPolicyFallbackEntry {
	provider: string;
	model_id: string;
}

export interface ForvenModelPolicyResponse {
	primary_provider: 'minimax' | 'openai' | string;
	primary_model: string;
	provider_priority: string[];
	default_models: Record<string, string>;
	fallback_chains: Record<string, ForvenModelPolicyFallbackEntry[]>;
}

/**
 * Payload for `PUT /api/model-policy`. All fields optional so individual
 * routing surfaces (Routing & Fallbacks tab) can patch just what they own.
 * - `provider_priority`: ordered list of providers tried for the primary/Brain slot.
 * - `default_models`: per-provider default model id.
 * - `fallback_chains`: per-slot ordered fallback list; an empty array means
 *   "no fallback (fail closed)".
 */
export interface ForvenModelPolicyUpdatePayload {
	primary_provider?: string;
	primary_model?: string;
	provider_priority?: string[];
	default_models?: Record<string, string>;
	fallback_chains?: Record<string, ForvenModelPolicyFallbackEntry[]>;
}

export interface ForvenAgentTerminalResponse {
	memory?: string;
	logs?: Array<{
		id?: number;
		level?: string;
		source?: string;
		message?: string;
		msg?: string;
		created_at?: string;
		ts?: string;
		data?: Record<string, unknown> | null;
	}>;
	[key: string]: unknown;
}

export interface ForvenBridgeStatus {
	available?: boolean;
	base_url?: string;
	remote_enabled?: boolean;
	remote_error?: string;
	error?: string;
	runs?: unknown[];
	outcomes?: Record<string, unknown>;
}

export interface SystemStatusResponse {
	paused: boolean;
	paused_at: string | null;
	generation_paused?: boolean;
	generation_paused_at?: string | null;
	system_mode?: SystemMode;
	system_mode_at?: string | null;
	paused_manual_counts?: PausedManualCounts;
}

export interface TradingResetResponse {
	ok: boolean;
	paused: boolean;
	paused_at: string | null;
	trading_allowed: boolean;
	trading_reason: string;
	reset: {
		system_pause_cleared: boolean;
		kill_switch_cleared: boolean;
		daily_loss_halt_cleared: boolean;
	};
	risk: {
		kill_switch_active: boolean;
		daily_loss_halt: boolean;
		high_water_mark: number;
		equity: number;
	};
}

export interface SchedulerReconcileResponse {
	ok: boolean;
	before: number;
	after: number;
	added: number;
	removed: number;
	monitoring_added: number;
}

export interface ManualScannerRunResponse {
	ok: boolean;
	mode: 'signal_only' | 'signal_execution' | string;
	execution_enabled: boolean;
	strategy_count: number;
	signals_count: number;
	actions_count: number;
	last_scan: string | null;
	last_signal_scan: string | null;
	last_execution_scan: string | null;
	last_execution_actions_count: number;
}

export interface ManualExchangeReconcileResponse {
	ok: boolean;
	sqlite_open: number;
	exchange_open: number;
	synced: boolean;
	discrepancy_count: number;
	discrepancies: Array<{
		type?: string;
		details?: string;
		[key: string]: unknown;
	}>;
}

export interface ForvenNotification {
	id: number;
	group_key: string;
	event_type: string;
	severity: 'info' | 'warn' | 'fail' | 'critical' | string;
	source: string;
	title: string;
	summary?: string | null;
	body?: string | null;
	status: string;
	delivery_mode?: string;
	resolved_channel_name?: string | null;
	resolved_channel_id?: string | null;
	dedupe_key?: string | null;
	metadata?: Record<string, unknown> | null;
	created_at: string;
	delivered_at?: string | null;
	acknowledged_at?: string | null;
	delivery_error?: string | null;
	repair_task?: NotificationRepairTaskSummary | null;
}

export interface NotificationFeedStats {
	lookback_hours: number;
	recent_total: number;
	counts: Record<string, number>;
}

export interface NotificationPreferences {
	discord_mode: 'legacy' | 'shadow' | 'policy' | string;
	response_channels: string[];
	approval_required_to_discord: boolean;
	approval_resolved_to_discord: boolean;
	trade_opened_to_discord: boolean;
	trade_closed_to_discord: boolean;
	trade_failed_to_discord: boolean;
	agent_completion_to_discord: boolean;
	agent_failure_to_discord: boolean;
	pipeline_transition_to_discord: boolean;
	system_degraded_to_discord: boolean;
	system_recovered_to_discord: boolean;
	risk_critical_to_discord: boolean;
	brain_response_to_discord: boolean;
	digests_to_discord: boolean;
}

export interface NotificationDeliveryAttempt {
	id: number;
	notification_id: number;
	target: string;
	delivery_mode: string;
	channel_name?: string | null;
	channel_id?: string | null;
	status: string;
	detail?: string | null;
	created_at: string;
}

export interface NotificationRepairTaskSummary {
	id: number;
	display_id: string;
	agent_id?: string | null;
	status: string;
	title?: string | null;
	created_at?: string | null;
	started_at?: string | null;
	completed_at?: string | null;
	error?: string | null;
}

export interface NotificationFeedResponse {
	items: ForvenNotification[];
	stats: NotificationFeedStats;
	preferences: NotificationPreferences;
}

export interface NotificationGroup {
	group_key: string;
	event_type: string;
	count: number;
	unacknowledged_count: number;
	highest_severity: 'info' | 'warn' | 'fail' | 'critical' | string;
	latest_item: ForvenNotification;
}

export interface NotificationGroupPagination {
	limit: number;
	has_more: boolean;
	next_cursor: string | null;
}

export interface NotificationGroupedResponse {
	groups: NotificationGroup[];
	pagination: NotificationGroupPagination;
	stats: NotificationFeedStats;
	preferences: NotificationPreferences;
}

export interface NotificationDeliveryHistoryResponse {
	notification_id: number;
	items: NotificationDeliveryAttempt[];
}

export interface NotificationRepairTaskResponse {
	ok: boolean;
	notification_id: number;
	agent_id: string;
	created: boolean;
	task: NotificationRepairTaskSummary;
}

export interface NotificationBulkAcknowledgeResponse {
	ok: boolean;
	count: number;
	items: ForvenNotification[];
}

export async function getForvenStats(): Promise<Record<string, number>> {
	return fetchApi('/forven/stats');
}

export async function getForvenDashboard(): Promise<ForvenDashboardResponse> {
	// Canonical endpoint. `/forven/dashboard` is a deprecated shim that proxies to
	// the same control-plane handler; the canonical route uses a non-strict account
	// fetch (no 503 when the exchange is unreachable) but returns the identical shape.
	return fetchApi('/api/dashboard');
}

export async function getForvenRisk(): Promise<ForvenRiskStatus> {
	// Canonical endpoint. `/forven/risk` is a deprecated shim that proxies to the
	// same control-plane handler and returns the identical shape.
	return fetchApi('/api/risk');
}

export async function getForvenRegime(): Promise<ForvenRegimeSnapshot> {
	return fetchApi('/forven/regime');
}

export async function getForvenSentiment(): Promise<ForvenSentimentSnapshot> {
	return fetchApi('/forven/sentiment');
}

export async function getForvenOpenTrades(): Promise<ForvenTrade[]> {
	return fetchApi('/forven/trades/open');
}

export async function getForvenRecentTrades(limit = 20): Promise<ForvenTrade[]> {
	return fetchApi(`/forven/trades/recent?limit=${limit}`);
}

export interface LiveSignalsResponse {
	strategy_id: string;
	indicators: Record<string, { name: string; value: number; timestamp: string }>;
	pending_signals: PendingSignal[];
	last_signal: 'entry' | 'exit' | 'none' | string;
	last_scan: string;
}

// Indicator series + display config for a live/deployed strategy's chart
// (mirrors the paper getSessionIndicators contract).
export async function getLiveIndicators(
	strategyId: string,
	timeframe?: string,
	limit = 500
): Promise<SessionIndicatorsResponse> {
	const params = new URLSearchParams();
	if (timeframe) params.set('timeframe', timeframe);
	params.set('limit', String(limit));
	return fetchApi(`/strategies/${encodeURIComponent(strategyId)}/live-indicators?${params}`);
}

// Entry/exit/blocked chart markers for a live/deployed strategy.
export async function getLiveMarkers(
	strategyId: string,
	limit = 500,
	includeGenerated = false
): Promise<TradeMarkersResponse> {
	const params = new URLSearchParams();
	params.set('limit', String(limit));
	if (includeGenerated) params.set('include_generated', 'true');
	return fetchApi(`/strategies/${encodeURIComponent(strategyId)}/live-markers?${params}`);
}

// Runtime indicators + pending ('approaching') signals for a live strategy.
export async function getLiveSignals(strategyId: string): Promise<LiveSignalsResponse> {
	return fetchApi(`/strategies/${encodeURIComponent(strategyId)}/live-signals`);
}

export async function forceCloseForvenTrade(
	tradeId: string,
	reason = 'Manual force close from Live Trades page'
): Promise<{
	ok: boolean;
	trade_id: string;
	asset: string;
	direction: string;
	close_side: string;
	exit_price: number | null;
	pnl_pct: number | null;
	pnl_usd: number | null;
	closed_at: string | null;
	source?: 'sqlite' | 'exchange';
	cancelled_reduce_only_orders?: number;
	cancel_error?: string | null;
}> {
	return fetchApi(`/forven/trades/${encodeURIComponent(tradeId)}/force-close`, {
		method: 'POST',
		body: JSON.stringify({ reason }),
	});
}

function tradesQueryParams(opts: ForvenTradesQuery): URLSearchParams {
	const params = new URLSearchParams();
	if (opts.status) params.set('status', opts.status);
	if (opts.asset) params.set('asset', opts.asset);
	if (opts.strategy) params.set('strategy', opts.strategy);
	if (opts.direction) params.set('direction', opts.direction);
	if (opts.execution_type) params.set('execution_type', opts.execution_type);
	if (opts.opened_from) params.set('opened_from', opts.opened_from);
	if (opts.opened_to) params.set('opened_to', opts.opened_to);
	if (opts.search) params.set('search', opts.search);
	return params;
}

export async function getForvenAllTrades(opts: ForvenTradesQuery = {}): Promise<ForvenTradesPage> {
	const params = tradesQueryParams(opts);
	params.set('limit', String(opts.limit ?? 200));
	params.set('offset', String(opts.offset ?? 0));
	if (opts.sort) params.set('sort', opts.sort);
	if (opts.sort_dir) params.set('sort_dir', opts.sort_dir);
	return fetchApi(`/api/trades?${params.toString()}`);
}

export async function getForvenTradesStats(
	opts: Omit<ForvenTradesQuery, 'limit' | 'offset' | 'sort' | 'sort_dir'> = {}
): Promise<ForvenTradesStats> {
	const params = tradesQueryParams(opts);
	const qs = params.toString();
	return fetchApi(`/api/trades/stats${qs ? `?${qs}` : ''}`);
}

export async function markForvenTradeFailed(
	tradeId: string,
	reason = 'Manually marked FAILED from All Trades page'
): Promise<{ ok: boolean; trade_id: string; status: string; reason: string }> {
	return fetchApi(`/api/trades/${encodeURIComponent(tradeId)}/mark-failed`, {
		method: 'POST',
		body: JSON.stringify({ reason }),
	});
}

export async function getForvenScannerState(): Promise<ForvenScannerState> {
	return fetchApi('/forven/scanner/state');
}

export async function getForvenStrategyPerformance(): Promise<ForvenStrategyPerformance[]> {
	return fetchApi('/forven/strategies/performance');
}

export async function getForvenAgentTasks(): Promise<ForvenAgentTask[]> {
	return fetchApi('/forven/agent-tasks');
}

export async function dismissForvenAgentTask(
	taskId: string | number,
	source: 'agent_tasks' | 'tasks' = 'agent_tasks',
	note?: string
): Promise<{ ok: boolean; id: string; source: string; status: string }> {
	return fetchApi(`/agent-tasks/${encodeURIComponent(String(taskId))}/dismiss`, {
		method: 'POST',
		body: JSON.stringify({ source, note })
	});
}

export async function getForvenSchedulerJobs(): Promise<ForvenSchedulerJob[]> {
	return fetchApi('/forven/scheduler');
}

export async function processAgentTaskQueues(
	options: QueueProcessingOptions = {}
): Promise<QueueProcessingResult> {
	return fetchApi('/agent-tasks/process', {
		method: 'POST',
		body: JSON.stringify(options),
	});
}

export async function updateForvenSchedulerJob(
	jobId: string | number,
	scheduleType: string,
	scheduleExpr: string,
	enabled?: boolean
): Promise<{ ok: boolean; error?: string }> {
	return fetchApi(`/scheduler/${jobId}`, {
		method: 'PATCH',
		body: JSON.stringify({ schedule_type: scheduleType, schedule_expr: scheduleExpr, enabled }),
	});
}

export async function getForvenAgents(): Promise<ForvenAgent[]> {
	return fetchApi('/forven/agents');
}

export interface ForvenAgentSpendTotal {
	agent_id: string;
	tasks: number;
	cost_usd: number;
	input_tokens: number;
	output_tokens: number;
}

/** Per-agent daily spend rollup — survives run-row pruning. */
export async function getForvenAgentSpend(days = 30): Promise<{
	days: number;
	daily: Array<Record<string, unknown>>;
	totals: ForvenAgentSpendTotal[];
}> {
	return fetchApi(`/agents/spend?days=${days}`);
}

export async function getForvenAgentDocuments(agentId: string): Promise<ForvenAgentDocumentsResponse> {
	return fetchApi(`/forven/agents/${encodeURIComponent(agentId)}/documents`);
}

export async function updateForvenAgent(agentId: string, payload: ForvenAgentUpdatePayload): Promise<ForvenAgent> {
	return fetchApi(`/forven/agents/${encodeURIComponent(agentId)}`, {
		method: 'PATCH',
		body: JSON.stringify(payload),
	});
}

export interface ForvenCreateStrategyDeveloperPayload {
	name: string;
	model?: ForvenProvider;
	model_id?: string;
	instructions?: string;
}

export async function createForvenStrategyDeveloperAgent(
	payload: ForvenCreateStrategyDeveloperPayload
): Promise<ForvenAgent> {
	return fetchApi('/forven/agents/strategy-developers', {
		method: 'POST',
		body: JSON.stringify(payload),
	});
}

export async function deleteForvenAgent(agentId: string): Promise<{ ok: boolean; deleted_agent_id: string }> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}`, {
		method: 'DELETE',
	});
}

export async function updateForvenAgentDocument(agentId: string, document: 'soul' | 'agents' | 'role', content: string): Promise<{ ok: boolean }> {
	return fetchApi(`/forven/agents/${encodeURIComponent(agentId)}/documents/${document}`, {
		method: 'PUT',
		body: JSON.stringify({ content }),
	});
}

export async function testForvenAgentDiscord(
	agentId: string,
	discordToken?: string
): Promise<ForvenAgentDiscordTestResponse> {
	const token = (discordToken ?? '').trim();
	return fetchApi(`/forven/agents/${encodeURIComponent(agentId)}/test-discord`, {
		method: 'POST',
		body: JSON.stringify(token ? { discord_token: token } : {}),
	});
}

export async function getForvenAgentModelOptions(refresh = false): Promise<ForvenAgentModelOptionsResponse> {
	const params = refresh ? '?refresh=true' : '';
	return fetchApi(`/forven/agents/model-options${params}`);
}

export async function getForvenAuthProviders(): Promise<ForvenAuthProvidersResponse> {
	return fetchApi('/forven/auth/providers');
}

export async function setForvenAuthProvider(
	provider: string,
	payload: ForvenAuthProviderProfilePayload
): Promise<{ ok: boolean; provider: string }> {
	return fetchApi(`/forven/auth/providers/${encodeURIComponent(provider)}`, {
		method: 'POST',
		body: JSON.stringify(payload),
	});
}

export async function startForvenAuthProviderOAuth(provider: string): Promise<ForvenAuthProviderOAuthStartResponse> {
	return fetchApi(`/forven/auth/providers/${encodeURIComponent(provider)}/oauth/start`, {
		method: 'POST'
	});
}

export async function completeForvenAuthProviderOAuth(
	provider: string,
	payload: ForvenAuthProviderOAuthCompleteBody
): Promise<ForvenAuthProviderOAuthCompleteResponse> {
	return fetchApi(`/forven/auth/providers/${encodeURIComponent(provider)}/oauth/complete`, {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export async function pollForvenAuthProviderOAuth(
	provider: string,
	state: string
): Promise<ForvenAuthProviderOAuthStatus> {
	return fetchApi(
		`/forven/auth/providers/${encodeURIComponent(provider)}/oauth/status?state=${encodeURIComponent(state)}`
	);
}

export async function cancelForvenAuthProviderOAuth(
	provider: string,
	state: string
): Promise<{ ok: boolean; provider: string }> {
	return fetchApi(
		`/forven/auth/providers/${encodeURIComponent(provider)}/oauth/cancel?state=${encodeURIComponent(state)}`,
		{ method: 'POST' }
	);
}

export async function deleteForvenAuthProvider(provider: string): Promise<{ ok: boolean; provider: string; removed?: boolean }> {
	return fetchApi(`/forven/auth/providers/${encodeURIComponent(provider)}`, {
		method: 'DELETE',
	});
}

export async function testForvenAuthProvider(provider: string): Promise<{ ok: boolean; provider: string; status: string; message?: string }> {
	return fetchApi(`/forven/auth/providers/${encodeURIComponent(provider)}/test`, {
		method: 'POST',
	});
}

export async function getForvenModelPolicy(): Promise<ForvenModelPolicyResponse> {
	return fetchApi('/forven/model-policy');
}

/**
 * Persist the model routing policy. Sends `provider_priority`, `default_models`
 * and `fallback_chains` (plus optional primary fields) to `PUT /api/model-policy`.
 */
export async function updateForvenModelPolicy(
	payload: ForvenModelPolicyUpdatePayload
): Promise<ForvenModelPolicyResponse> {
	return fetchApi('/forven/model-policy', {
		method: 'PUT',
		body: JSON.stringify(payload),
	});
}

export async function updateForvenAgentModel(
	agentId: string,
	payload: { model: ForvenProvider; model_id?: string }
): Promise<ForvenAgent> {
	return fetchApi(`/forven/agents/${encodeURIComponent(agentId)}/model`, {
		method: 'PATCH',
		body: JSON.stringify(payload),
	});
}

export async function getForvenBacktestingStatus(): Promise<ForvenBridgeStatus> {
	return fetchApi('/forven/backtesting/status');
}

export async function getSystemStatus(): Promise<SystemStatusResponse> {
	return fetchApi('/system/status');
}

export async function getStrategyGenerationStatus(): Promise<{
	ok: boolean;
	generation_paused: boolean;
	generation_paused_at: string | null;
}> {
	return fetchApi('/system/generation/status');
}

export async function getNotificationFeed(params: {
	limit?: number;
	status?: string;
	severity?: string;
	source?: string;
	event_type?: string;
	group_key?: string;
	before_id?: number;
	/** Server-side filter matching the nav-badge "actionable issues" rules. */
	actionable?: boolean;
} = {}): Promise<NotificationFeedResponse> {
	const search = new URLSearchParams();
	if (params.limit !== undefined) search.set('limit', String(params.limit));
	if (params.status) search.set('status', params.status);
	if (params.severity) search.set('severity', params.severity);
	if (params.source) search.set('source', params.source);
	if (params.event_type) search.set('event_type', params.event_type);
	if (params.group_key) search.set('group_key', params.group_key);
	if (params.before_id !== undefined) search.set('before_id', String(params.before_id));
	if (params.actionable) search.set('actionable', 'true');
	const query = search.toString();
	return fetchApi(`/notifications${query ? `?${query}` : ''}`);
}

export async function getNotificationFeedGrouped(params: {
	limit?: number;
	status?: string;
	severity?: string;
	source?: string;
	event_type?: string;
	cursor?: string;
} = {}): Promise<NotificationGroupedResponse> {
	const search = new URLSearchParams();
	if (params.limit !== undefined) search.set('limit', String(params.limit));
	if (params.status) search.set('status', params.status);
	if (params.severity) search.set('severity', params.severity);
	if (params.source) search.set('source', params.source);
	if (params.event_type) search.set('event_type', params.event_type);
	if (params.cursor) search.set('cursor', params.cursor);
	const query = search.toString();
	return fetchApi(`/notifications/grouped${query ? `?${query}` : ''}`);
}

export async function acknowledgeNotification(notificationId: number): Promise<{ ok: boolean; item: ForvenNotification }> {
	return fetchApi(`/notifications/${notificationId}/acknowledge`, { method: 'POST' });
}

export async function acknowledgeNotifications(
	notificationIds: number[],
): Promise<NotificationBulkAcknowledgeResponse> {
	return fetchApi('/notifications/acknowledge-all', {
		method: 'POST',
		body: JSON.stringify({ ids: notificationIds }),
	});
}

export async function createNotificationRepairTask(
	notificationId: number,
	agentId = 'full-stack-engineer'
): Promise<NotificationRepairTaskResponse> {
	return fetchApi(`/notifications/${notificationId}/repair-task`, {
		method: 'POST',
		body: JSON.stringify({ agent_id: agentId }),
	});
}

export async function getNotificationDeliveries(
	notificationId: number,
	limit = 20
): Promise<NotificationDeliveryHistoryResponse> {
	return fetchApi(`/notifications/${notificationId}/deliveries?limit=${limit}`);
}

export async function resendNotification(notificationId: number): Promise<{ ok: boolean; item: ForvenNotification }> {
	return fetchApi(`/notifications/${notificationId}/resend`, { method: 'POST' });
}

export async function getNotificationPreferences(): Promise<NotificationPreferences> {
	return fetchApi('/notifications/preferences');
}

export async function updateNotificationPreferences(
	updates: Partial<NotificationPreferences>
): Promise<NotificationPreferences> {
	return fetchApi('/notifications/preferences', {
		method: 'PUT',
		body: JSON.stringify({ updates }),
	});
}

export async function sendNotificationTest(
	eventType = 'system_degraded'
): Promise<{ ok: boolean; item: ForvenNotification }> {
	return fetchApi('/notifications/test', {
		method: 'POST',
		body: JSON.stringify({ event_type: eventType }),
	});
}

export async function stopSystem(): Promise<{ ok: boolean; paused: boolean }> {
	return fetchApi('/system/stop', { method: 'POST' });
}

export async function startSystem(): Promise<{ ok: boolean; paused: boolean }> {
	return fetchApi('/system/start', { method: 'POST' });
}

export async function pauseStrategyGeneration(): Promise<{
	ok: boolean;
	generation_paused: boolean;
	generation_paused_at: string | null;
}> {
	return fetchApi('/system/generation/pause', { method: 'POST' });
}

export async function resumeStrategyGeneration(): Promise<{
	ok: boolean;
	generation_paused: boolean;
	generation_paused_at: string | null;
}> {
	return fetchApi('/system/generation/resume', { method: 'POST' });
}

export type SystemMode = 'manual' | 'semi_auto' | 'auto';

export type PausedManualCounts = {
	agent_tasks: number;
	brain_tasks: number;
	total: number;
};

export type SystemModeResponse = {
	ok: boolean;
	system_mode: SystemMode;
	system_mode_at: string | null;
	paused: boolean;
	generation_paused: boolean;
	paused_manual_counts: PausedManualCounts;
};

export async function getSystemMode(): Promise<SystemModeResponse> {
	return fetchApi('/system/mode');
}

export async function setSystemMode(mode: SystemMode): Promise<SystemModeResponse> {
	return fetchApi('/system/mode', {
		method: 'POST',
		body: JSON.stringify({ mode }),
	});
}

export async function resetTradingHalt(): Promise<TradingResetResponse> {
	return fetchApi('/system/trading/reset', {
		method: 'POST',
		body: JSON.stringify({ confirm: true }),
	});
}

/** EQ-BASIS-3: re-anchor HWM / daily start / last equity to a fresh books-aware live read. */
export async function rebaselineEquityAnchors(): Promise<{
	ok: boolean;
	equity: number;
	high_water_mark: number;
	previous_high_water_mark: number;
	daily_start_equity: number;
	source: string;
}> {
	return fetchApi('/risk/equity/rebaseline', {
		method: 'POST',
		body: JSON.stringify({ confirm: true }),
	});
}

export async function reconcileSchedulerJobs(): Promise<SchedulerReconcileResponse> {
	return fetchApi('/scheduler/reconcile', { method: 'POST' });
}

export interface AgentProviderWarning {
	agent_id: string;
	provider: string;
	fallback: string | null;
}

/**
 * Runtime health for a single provider as observed by the backend during live
 * agent/Brain calls. Backend (`GET /api/agents/provider-health`) is being
 * updated to emit a `runtime` array of these alongside the existing
 * `warnings`. Treat every field except `provider`/`state` as optional and
 * degrade gracefully — the page must never crash if the shape is incomplete.
 */
export interface ProviderRuntimeHealth {
	provider: string;
	state: 'ok' | 'degraded' | 'down' | string;
	kind?: 'rate_limit' | 'quota' | 'auth' | 'transient' | 'fallback' | string;
	message?: string | null;
	// Backend emits these as epoch SECONDS (numbers), though some shapes send an
	// ISO string — accept both (and null) so consumers can normalize.
	since?: string | number | null;
	last_event_at?: string | number | null;
	last_ok_at?: string | number | null;
	fallback_to?: string | null;
}

export interface ProviderHealthResponse {
	warnings: AgentProviderWarning[];
	runtime: ProviderRuntimeHealth[];
	count?: number;
}

export async function getAgentProviderHealth(): Promise<{
	warnings: AgentProviderWarning[];
	runtime?: ProviderRuntimeHealth[];
	count: number;
}> {
	return fetchApi('/agents/provider-health');
}

/**
 * Typed wrapper over `GET /api/agents/provider-health` that always returns the
 * `{warnings, runtime}` shape the Health tab and ConnectionHealthBanner depend
 * on, defaulting both arrays so consumers can map over them unconditionally.
 */
export async function getProviderHealth(): Promise<ProviderHealthResponse> {
	const res = await fetchApi<Partial<ProviderHealthResponse>>('/agents/provider-health');
	return {
		warnings: Array.isArray(res?.warnings) ? res.warnings : [],
		runtime: Array.isArray(res?.runtime) ? res.runtime : [],
		count: typeof res?.count === 'number' ? res.count : undefined,
	};
}

export async function reconcileAgentProviders(): Promise<{
	updated: number;
	provider: string | null;
	model_id: string | null;
	agents: string[];
}> {
	return fetchApi('/agents/reconcile-providers', { method: 'POST' });
}

export async function runSignalScanNow(): Promise<ManualScannerRunResponse> {
	return fetchApi('/system/scanner/signal-run', { method: 'POST' });
}

export async function runExecutionScanNow(): Promise<ManualScannerRunResponse> {
	return fetchApi('/system/scanner/execution-run', { method: 'POST' });
}

export async function reconcileExchangeNow(): Promise<ManualExchangeReconcileResponse> {
	return fetchApi('/system/exchange/reconcile', { method: 'POST' });
}

export async function setForvenExecutionMode(mode: 'paper' | 'live' | 'mainnet'): Promise<{ ok: boolean; mode: string; previous: string }> {
	return fetchApi('/forven/execution-mode', {
		method: 'POST',
		body: JSON.stringify({ mode, confirm: true }),
	});
}

export async function triggerForvenEmergencyHalt(): Promise<{ ok: boolean; closed?: unknown[] }> {
	return fetchApi('/forven/emergency-halt', {
		method: 'POST',
		body: JSON.stringify({ confirm: true }),
	});
}

export async function resetForvenKillSwitch(): Promise<{ ok: boolean }> {
	return fetchApi('/forven/kill-switch/reset', {
		method: 'POST',
		body: JSON.stringify({ confirm: true }),
	});
}

export async function toggleForvenKillSwitch(enabled: boolean): Promise<{ ok: boolean; kill_switch_enabled: boolean }> {
	return fetchApi('/forven/kill-switch/toggle', {
		method: 'POST',
		body: JSON.stringify({ enabled }),
	});
}

export interface EmergencyHaltResponse {
	ok: boolean;
	error?: string;
	closed?: unknown[];
}

export interface KillSwitchToggleResponse {
	ok: boolean;
	error?: string;
	kill_switch_enabled?: boolean;
}

/**
 * Trigger an emergency halt: close all open positions immediately.
 * Canonical endpoint (`POST /api/emergency-halt`); requires confirm=true.
 */
export async function triggerEmergencyHalt(): Promise<EmergencyHaltResponse> {
	return fetchApi('/api/emergency-halt', {
		method: 'POST',
		body: JSON.stringify({ confirm: true }),
	});
}

/**
 * Enable or disable the automatic kill-switch trigger.
 * Canonical endpoint (`POST /api/kill-switch/toggle`). Enabling does NOT
 * trip the switch; it re-arms drawdown auto-halting. Disabling stops the
 * automatic trigger (manual halts/resets are unaffected).
 */
export async function setKillSwitchEnabled(enabled: boolean): Promise<KillSwitchToggleResponse> {
	return fetchApi('/api/kill-switch/toggle', {
		method: 'POST',
		body: JSON.stringify({ enabled }),
	});
}

function resolveWsApiKey(): string {
	// Mirror buildAuthHeaders() in core.ts: env first, then localStorage. Browsers
	// can't set WS handshake headers, so the key rides as a query param instead.
	try {
		const fromEnv = String((import.meta as { env?: Record<string, unknown> }).env?.VITE_FORVEN_API_KEY ?? '').trim();
		if (fromEnv) return fromEnv;
	} catch {
		/* ignore */
	}
	try {
		if (typeof window !== 'undefined') {
			return (window.localStorage.getItem('forven_api_key') ?? '').trim();
		}
	} catch {
		/* ignore */
	}
	return '';
}

function toLiveWsUrl(base: string): string {
	let wsBase = base.replace('http://', 'ws://').replace('https://', 'wss://');
	if (base.startsWith('/')) {
		const protocol = typeof window !== 'undefined' && window.location?.protocol === 'https:' ? 'wss:' : 'ws:';
		const host = typeof window !== 'undefined' && window.location?.host
			? window.location.host
			: '127.0.0.1:8003';
		wsBase = `${protocol}//${host}${base}`;
	}
	// SECURITY (audit 2026-06-22, L3): when an API key is configured, pass it so
	// the WS handshake authorizes. Omitted entirely when no key is set, so the
	// default localhost URL stays clean.
	const apiKey = resolveWsApiKey();
	const query = apiKey ? `?key=${encodeURIComponent(apiKey)}` : '';
	return `${wsBase}/ws/live${query}`;
}

export function getForvenLiveWebSocketUrls(): string[] {
	const candidates = new Set<string>();
	const active = (ACTIVE_API_BASE || '').trim();
	const primary = (API_BASE || '').trim();
	const preferredAbsoluteBases = [primary, active].filter(
		(base) => Boolean(base) && !String(base).startsWith('/')
	);

	// Prefer direct backend origins over dev-proxy `/api` and avoid speculative
	// fallbacks that can bounce the client onto an invalid WS endpoint.
	for (const base of preferredAbsoluteBases) {
		candidates.add(base);
	}

	if (typeof window !== 'undefined' && window.location) {
		const protocol = window.location.protocol || 'http:';
		const host = window.location.hostname || '127.0.0.1';
		candidates.add(`${protocol}//${host}:8003/api`);
		if (preferredAbsoluteBases.length === 0) {
			candidates.add(`${window.location.origin.replace(/\/$/, '')}/api`);
		}
	}

	if (preferredAbsoluteBases.length === 0) {
		if (primary) candidates.add(primary);
		if (active) candidates.add(active);
		candidates.add('/api');
	}

	return Array.from(candidates)
		.map((base) => String(base || '').trim())
		.filter(Boolean)
		.map((base) => toLiveWsUrl(base));
}

export function getForvenLiveWebSocketUrl(): string {
	const urls = getForvenLiveWebSocketUrls();
	return urls[0] ?? toLiveWsUrl(ACTIVE_API_BASE || API_BASE || '/api');
}

export async function getForvenEquityHistory(): Promise<ForvenEquityHistory> {
	return fetchApi('/forven/equity-history');
}

export interface ForvenLogEntry {
	level: string;
	source?: string | null;
	ts?: string | null;
	created_at?: string | null;
	msg?: string | null;
	message?: string | null;
	meta?: Record<string, unknown>;
}

export async function getForvenLogs(limit = 100): Promise<ForvenLogEntry[]> {
	return fetchApi(`/forven/logs?limit=${limit}`);
}

export async function getForvenAgentTerminal(agentId: string): Promise<ForvenAgentTerminalResponse> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}/terminal`);
}

export async function getForvenEvolution(): Promise<Record<string, number>> {
	return fetchApi('/forven/evolution');
}

export async function getForvenStrategies(status?: string): Promise<Array<Record<string, unknown>>> {
	const params = status ? `?status=${encodeURIComponent(status)}` : '';
	return fetchApi(`/forven/strategies${params}`);
}

export interface ForvenStrategyQuery {
	status?: string;
	owner?: string;
	limit?: number;
	offset?: number;
}

export async function getForvenStrategiesQuery(query: ForvenStrategyQuery = {}): Promise<Array<Record<string, unknown>>> {
	const params = new URLSearchParams();
	if (query.status) params.set('status', query.status);
	if (query.owner) params.set('owner', query.owner);
	if (query.limit !== undefined) params.set('limit', String(query.limit));
	if (query.offset !== undefined) params.set('offset', String(query.offset));
	const queryString = params.toString();
	return fetchApi(`/forven/strategies${queryString ? `?${queryString}` : ''}`);
}

export type NowWorkingTask = {
	type: string;
	status: 'running' | 'pending' | 'paused_manual';
	started_at: string | null;
	stalled: boolean;
};

export type NowWorkingRow = {
	strategy_id: string;
	name: string;
	stage: string | null;
	current_task: NowWorkingTask;
	since: string | null;
};

export async function getNowWorking(): Promise<NowWorkingRow[]> {
	return fetchApi('/lab/now-working');
}

export async function promoteForvenStrategy(
	strategyId: string,
	toStatus: string,
	options?: {
		fromStatus?: string;
		fromOwner?: string;
		reason?: string;
		force?: boolean;
		override?: boolean;
		/** GO-LIVE-1: typed confirmation phrase ("GO LIVE") required to land in live_graduated. */
		confirm?: string;
		/** GO-LIVE-1: initial per-asset notional ceiling (USD), enforced per live order. */
		liveNotionalCeilingUsd?: number;
	}
): Promise<{
	ok: boolean;
	strategy_id: string;
	from_status: string;
	to_status: string;
	updated_at: string;
}> {
	const result = await fetchApi<{
		ok?: boolean;
		error?: unknown;
		strategy_id?: string;
		from_status?: string;
		to_status?: string;
		updated_at?: string;
	}>(`/forven/strategies/${encodeURIComponent(strategyId)}/promote`, {
		method: 'POST',
		body: JSON.stringify({
			to_status: toStatus,
			from_status: options?.fromStatus,
			from_owner: options?.fromOwner,
			reason: options?.reason,
			force: options?.force ?? false,
			override: options?.override ?? false,
			confirm: options?.confirm,
			live_notional_ceiling_usd: options?.liveNotionalCeilingUsd,
		}),
	});
	if (result?.ok === false) {
		const message = typeof result.error === 'string' && result.error.trim()
			? result.error.trim()
			: 'Strategy transition failed';
		throw new Error(message);
	}
	return {
		ok: Boolean(result?.ok ?? true),
		strategy_id: String(result?.strategy_id ?? strategyId),
		from_status: String(result?.from_status ?? ''),
		to_status: String(result?.to_status ?? toStatus),
		updated_at: String(result?.updated_at ?? new Date().toISOString()),
	};
}

/** GO-LIVE-1: set (or clear, with null) a strategy's per-asset live notional ceiling. */
export async function setLiveNotionalCeiling(
	strategyId: string,
	ceilingUsd: number | null
): Promise<{ ok: boolean; strategy_id: string; ceiling: Record<string, unknown> | null }> {
	return fetchApi(`/strategies/${encodeURIComponent(strategyId)}/live-ceiling`, {
		method: 'PUT',
		body: JSON.stringify({ ceiling_usd: ceilingUsd }),
	});
}

export interface StrategyHandoffPayload {
	toOwner: string;
	toStatus?: string;
	fromStatus?: string;
	fromOwner?: string;
	reason?: string;
	append?: boolean;
}

export async function handoffStrategy(
	strategyId: string,
	payload: StrategyHandoffPayload
): Promise<{
	ok: boolean;
	strategy_id: string;
	from_owner: string;
	to_owner: string;
	from_status: string;
	to_status: string;
	updated_at: string;
}> {
	return fetchApi(`/strategies/${encodeURIComponent(strategyId)}/handoff`, {
		method: 'POST',
		body: JSON.stringify({
			to_status: payload.toStatus,
			to_owner: payload.toOwner,
			from_status: payload.fromStatus,
			from_owner: payload.fromOwner,
			reason: payload.reason,
			append: payload.append ?? true,
		}),
	});
}

export type ApprovalStatus =
	| 'pending_approval'
	| 'approved'
	| 'denied'
	| 'revised'
	| 'failed'
	| 'pending';

export interface ApprovalRecord {
	id: number;
	approval_type: string;
	target_type: string;
	target_id: string | null;
	requested_status: string | null;
	status: ApprovalStatus;
	actor: string | null;
	reason: string | null;
	payload: Record<string, unknown> | null;
	feedback: string | null;
	decision: string | null;
	error: string | null;
	owner: string;
	created_at: string;
	updated_at: string;
	decided_at: string | null;
	linked_task?: ApprovalTaskSummary | null;
	troubleshoot_task?: ApprovalTaskSummary | null;
	can_troubleshoot?: boolean;
	can_watch_execution?: boolean;
	expires_at?: string | null;
	classifier_recommendation?: 'auto_approve' | 'escalate' | 'hold' | null;
	classifier_reasoning?: string | null;
	classifier_model?: string | null;
	classifier_at?: string | null;
	auto_approved?: number | boolean | null;
	escalated_at?: string | null;
	escalated_to?: string | null;
}

export interface ApprovalModesSettings {
	modes: Record<string, string>;
	default_mode: string;
	deadlines_hours: Record<string, number>;
	default_deadline_hours: number;
	escalation_owner: string;
	valid_modes: string[];
	off_allowlist: string[];
	known_categories: string[];
}

export interface ApprovalModesPayload {
	modes?: Record<string, string>;
	default_mode?: string;
	deadlines_hours?: Record<string, number>;
	default_deadline_hours?: number;
	escalation_owner?: string;
}

export interface BulkApproveResponse {
	approved: number[];
	skipped: number[];
	missing: number[];
}

export interface ApprovalDecisionPayload {
	actor?: string;
	feedback?: string;
	reason?: string;
	/** GO-LIVE-1: typed confirmation phrase ("GO LIVE"), required when approving a live promotion. */
	confirm?: string;
	/** GO-LIVE-1: initial per-asset notional ceiling (USD), required when approving a live promotion. */
	live_notional_ceiling_usd?: number;
}

export interface ApprovalHandoffPayload {
	to_owner: string;
	reason?: string;
}

export interface ApprovalTroubleshootPayload {
	agent_id?: string;
}

export interface ApprovalListQuery {
	status?: string;
	approval_type?: string;
	target_type?: string;
	target_id?: string;
	owner?: string;
	limit?: number;
	offset?: number;
}

export interface ApprovalTaskSummary {
	id: number;
	display_id: string | null;
	agent_id?: string | null;
	type?: string | null;
	title?: string | null;
	description?: string | null;
	status: string;
	priority: number;
	strategy_id?: string | null;
	created_at?: string | null;
	started_at?: string | null;
	completed_at?: string | null;
	error?: string | null;
}

export interface ApprovalTaskDetail {
	task: TaskContainer;
	audit_log: TaskAuditEvent[];
	tool_calls: Array<Record<string, unknown>>;
}

/** Point-in-time strategy summary embedded in promotion/dethrone approval payloads
 *  (payload.strategy_snapshot). Everything optional: approvals created before
 *  2026-07-06 carry no snapshot. */
export interface StrategySnapshot {
	display_id?: string | null;
	name?: string | null;
	display_name?: string | null;
	symbol?: string | null;
	timeframe?: string | null;
	stage?: string | null;
	stage_changed_at?: string | null;
	backtest?: {
		sharpe?: number | null;
		total_return?: number | null;
		max_drawdown?: number | null;
		trades?: number | null;
	} | null;
	forward?: {
		window_days?: number | null;
		closed_trades?: number | null;
		realized_pnl_pct_sum?: number | null;
		wins?: number | null;
	} | null;
	captured_at?: string | null;
}

/** Live decision context returned by GET /approvals/{id}/context for
 *  strategy-targeted approvals. */
export interface ApprovalStrategyContext {
	strategy?: Record<string, unknown> | null;
	dethrone_history?: {
		pending?: number;
		approved?: number;
		denied?: number;
		expired?: number;
		last_denied_at?: string | null;
	} | null;
	cooldown?: {
		active?: boolean;
		until?: string | null;
		deny_count?: number;
	} | null;
}

export interface ApprovalContextResponse {
	approval: ApprovalRecord;
	linked_task?: ApprovalTaskSummary | null;
	troubleshoot_task?: ApprovalTaskSummary | null;
	linked_task_detail?: ApprovalTaskDetail | null;
	troubleshoot_task_detail?: ApprovalTaskDetail | null;
	strategy_context?: ApprovalStrategyContext | null;
	recommended_mode: 'diagnosis' | 'execution' | string;
}

export interface ApprovalTroubleshootResponse {
	ok: boolean;
	approval_id: number;
	agent_id: string;
	created: boolean;
	task: ApprovalTaskSummary;
}

export async function getApprovals(params: ApprovalListQuery = {}): Promise<ApprovalRecord[]> {
	const query = new URLSearchParams();
	if (params.status) query.set('status', params.status);
	if (params.approval_type) query.set('approval_type', params.approval_type);
	if (params.target_type) query.set('target_type', params.target_type);
	if (params.target_id) query.set('target_id', params.target_id);
	if (params.owner) query.set('owner', params.owner);
	if (params.limit !== undefined) query.set('limit', String(params.limit));
	if (params.offset !== undefined) query.set('offset', String(params.offset));
	return fetchApi(`/approvals${query ? `?${query}` : ''}`);
}

export async function getApprovalContext(approvalId: number): Promise<ApprovalContextResponse> {
	return fetchApi(`/approvals/${encodeURIComponent(String(approvalId))}/context`);
}

export async function handoffApproval(
	approvalId: number,
	payload: ApprovalHandoffPayload
): Promise<{ ok: boolean; approval_id: number; owner: string }> {
	return fetchApi(`/approvals/${encodeURIComponent(String(approvalId))}/handoff`, {
		method: 'POST',
		body: JSON.stringify(payload)
	});
}

export async function approveApproval(
	approvalId: number,
	payload?: ApprovalDecisionPayload
): Promise<{ ok: boolean; approval_id: number; status: ApprovalStatus; task_id?: number; task_display_id?: string | null }> {
	return fetchApi(`/approvals/${encodeURIComponent(String(approvalId))}/approve`, {
		method: 'POST',
		body: JSON.stringify(payload || {}),
	});
}

export async function denyApproval(
	approvalId: number,
	payload?: ApprovalDecisionPayload
): Promise<{ ok: boolean; approval_id: number; status: ApprovalStatus }> {
	return fetchApi(`/approvals/${encodeURIComponent(String(approvalId))}/deny`, {
		method: 'POST',
		body: JSON.stringify(payload || {}),
	});
}

export async function reviseApproval(
	approvalId: number,
	payload?: ApprovalDecisionPayload
): Promise<{ ok: boolean; approval_id: number; status: ApprovalStatus }> {
	return fetchApi(`/approvals/${encodeURIComponent(String(approvalId))}/revise`, {
		method: 'POST',
		body: JSON.stringify(payload || {}),
	});
}

export async function classifyApproval(approvalId: number): Promise<{
	recommendation: string;
	reasoning: string;
	confidence: number;
	model: string | null;
	latency_ms: number;
}> {
	return fetchApi(`/approvals/${encodeURIComponent(String(approvalId))}/classify`, {
		method: 'POST',
		body: JSON.stringify({}),
	});
}

export async function getApprovalModes(): Promise<ApprovalModesSettings> {
	return fetchApi('/approvals/modes');
}

export async function putApprovalModes(payload: ApprovalModesPayload): Promise<ApprovalModesSettings> {
	return fetchApi('/approvals/modes', {
		method: 'PUT',
		body: JSON.stringify(payload),
	});
}

export async function bulkApproveApprovals(
	approvalIds: number[],
	options: { actor?: string; feedback?: string } = {},
): Promise<BulkApproveResponse> {
	return fetchApi('/approvals/bulk-approve', {
		method: 'POST',
		body: JSON.stringify({
			approval_ids: approvalIds,
			actor: options.actor,
			feedback: options.feedback,
		}),
	});
}

export interface ToolsetOverrideRule {
	tool_name: string;
	enabled: boolean;
	updated_at?: string;
	updated_by?: string;
	context?: string;
	agent_id?: string;
}

export interface ToolsetEffectiveEntry {
	name: string;
	category: string;
	enabled: boolean;
	source: string;
}

export interface ToolDefinition {
	name: string;
	category: string;
	description?: string;
}

export interface AgentToolsetsResponse {
	agent_id: string;
	valid_contexts: string[];
	categories: string[];
	all_tools: ToolDefinition[];
	contexts: Record<string, { overrides: ToolsetOverrideRule[]; effective: ToolsetEffectiveEntry[] }>;
}

export async function getAgentToolsets(agentId: string): Promise<AgentToolsetsResponse> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}/toolsets`);
}

export async function putAgentToolsetOverrides(
	agentId: string,
	context: string,
	overrides: Array<{ tool_name: string; enabled: boolean }>,
): Promise<{
	agent_id: string;
	context: string;
	overrides: ToolsetOverrideRule[];
	effective: ToolsetEffectiveEntry[];
}> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}/toolsets/${encodeURIComponent(context)}`, {
		method: 'PUT',
		body: JSON.stringify({ overrides }),
	});
}

export async function deleteAgentToolsetOverrides(
	agentId: string,
	context: string,
): Promise<{ agent_id: string; context: string; deleted: number; effective: ToolsetEffectiveEntry[] }> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}/toolsets/${encodeURIComponent(context)}`, {
		method: 'DELETE',
	});
}

export async function troubleshootApproval(
	approvalId: number,
	payload?: ApprovalTroubleshootPayload
): Promise<ApprovalTroubleshootResponse> {
	return fetchApi(`/approvals/${encodeURIComponent(String(approvalId))}/troubleshoot`, {
		method: 'POST',
		body: JSON.stringify(payload || {}),
	});
}

export async function userCompleteApproval(
	approvalId: number,
	payload?: ApprovalDecisionPayload
): Promise<{ ok: boolean; approval_id: number; status: ApprovalStatus; task_id?: number; task_display_id?: string | null }> {
	return fetchApi(`/approvals/${encodeURIComponent(String(approvalId))}/user-complete`, {
		method: 'POST',
		body: JSON.stringify(payload || {}),
	});
}

// ============== Brain Chat ==============

export interface BrainChatSubmitResponse {
	ok: boolean;
	task_id: number;
	error?: string;
}

export interface BrainChatResultResponse {
	ok: boolean;
	status: string;
	result: { response?: string;[key: string]: unknown } | null;
	error: string | null;
	created_at: string | null;
	completed_at: string | null;
}

/**
 * Optional entity scoping for Brain chat. When the operator is viewing a
 * specific entity (e.g. a strategy detail page), passing entity_type +
 * entity_id lets the Brain resolve "this one" / "it" to that entity. The
 * backend (BrainChatBody) injects a "# USER CONTEXT" block from these.
 */
export interface BrainChatEntity {
	entity_type?: string;
	entity_id?: string;
}

export async function postBrainChat(
	message: string,
	context?: string,
	history?: { role: string; content: string }[],
	entity?: BrainChatEntity,
): Promise<BrainChatSubmitResponse> {
	const payload: Record<string, unknown> = { message };
	if (context) payload.context = context;
	if (history && history.length > 0) payload.history = history;
	if (entity?.entity_type && entity?.entity_id) {
		payload.entity_type = entity.entity_type;
		payload.entity_id = entity.entity_id;
	}
	return fetchApi('/brain/chat', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload),
	});
}

export async function getBrainChatResult(taskId: number): Promise<BrainChatResultResponse> {
	return fetchApi(`/brain/chat/${taskId}`);
}

/**
 * Stable error codes the direct chat path can return on failure so the UI can
 * render an actionable CTA (e.g. link to provider settings) instead of a raw
 * stack trace. Unknown/unspecified failures omit error_code.
 */
export type BrainChatErrorCode = 'provider_unconfigured' | 'provider_rate_limited';

export interface BrainChatDirectResponse {
	ok: boolean;
	response?: string;
	error?: string;
	error_code?: BrainChatErrorCode | string;
	retryable?: boolean;
	mode: string;
}

export async function postBrainChatDirect(
	message: string,
	context?: string,
	history?: { role: string; content: string }[],
	entity?: BrainChatEntity,
): Promise<BrainChatDirectResponse> {
	const payload: Record<string, unknown> = { message };
	if (context) payload.context = context;
	if (history && history.length > 0) payload.history = history;
	if (entity?.entity_type && entity?.entity_id) {
		payload.entity_type = entity.entity_type;
		payload.entity_id = entity.entity_id;
	}
	return fetchApi('/brain/chat/direct', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(payload),
	});
}


export interface ResearchMemoryMode {
	constraint_memory: boolean;
	inspiration_memory: string;
}

export interface HypothesisDisciplineSettings {
	active_pool_cap: number;
	min_strategies_per_pick: number;
	revisit_interval_days: number;
	verdict_hit_rate_threshold: number;
	verdict_min_diversity_cells: number;
	verdict_rolling_window: number;
	max_unrefined_active?: number;
	unstarted_ageout_days?: number;
	refine_in_flight_budget?: number;
	disproven_dedup_lookback_days?: number;
}

export interface AutonomousDiscoverySettings {
	enabled: boolean;
	mode: 'operator_approves' | 'autonomous';
	max_open_discovery_tasks: number;
}

export interface ResearchSettings {
	external_benchmarking_enabled: boolean;
	lane_weights: Record<string, number>;
	spawn_limits: {
		per_run: number;
		rolling_window: number;
		window_days: number;
	};
	memory_modes: Record<string, ResearchMemoryMode>;
	allowed_external_source_types: string[];
	autonomous_discovery?: AutonomousDiscoverySettings;
	hypothesis_discipline?: HypothesisDisciplineSettings;
	research_sources?: {
		reddit?: {
			enabled?: boolean;
			subs?: string[];
			client_id?: string | null;
			client_secret?: string | null;
			rate_limit_per_min?: number;
		};
		[sourceType: string]: unknown;
	};
}

export interface ForvenSettings {
	exchange: string;
	trading_mode: string;
	initial_capital: number;
	agent_model_keys: string[];
	hyperliquid_wallet: string;
	hyperliquid_api_address: string;
	hyperliquid_has_key: boolean;
	hyperliquid_testnet: boolean;
	// Enforced risk keys (exchange/risk.py reads these); the legacy twins below
	// are kept in sync by the backend risk-section writer.
	max_risk_per_trade_pct: number;
	max_daily_loss_pct: number;
	max_position_size_pct: number;
	max_daily_loss: number;
	max_drawdown_pct: number;
	max_concurrent_positions: number;
	cooldown_after_loss_hours: number;
	strategy_name: string;
	strategy_symbol: string;
	strategy_timeframe: string;
	strategy_parameters: Record<string, unknown>;
	self_healing_enabled: boolean;
	discord_bot_token_configured: boolean;
	discord_webhook_configured: boolean;
	notification_level: string;
	notify_on_entry: boolean;
	notify_on_exit: boolean;
	notify_daily_summary: boolean;
	notify_health_reports: boolean;
	notify_errors: boolean;
	auto_restart_on_crash: boolean;
	maintenance_start_hour: number | null;
	maintenance_end_hour: number | null;
	data_refresh_seconds: number;
	scanner_execution_enabled: boolean;
	throughput_auto_scheduler_control: boolean;
	adaptive_pipeline_throughput_enabled: boolean;
	pipeline_target_clear_hours: number;
	ideation_interval_minutes: number;
	coding_interval_minutes: number;
	testing_interval_minutes: number;
	graduation_interval_minutes: number;
	scanner_signal_interval_minutes: number;
	scanner_execution_interval_minutes: number;
	scanner_allow_direct_market_fetch: boolean;
	daemon_candle_cache_refresh_seconds: number;
	pipeline_assignments_per_cycle: number;
	pipeline_drain_mode: boolean;
	pipeline_drain_max_seconds: number;
	backtest_matrix_workers: number;
	pipeline_saturation_threshold: number;
	pipeline_resume_threshold: number;
	pipeline_gate_failure_archive_attempts: number;
	agent_task_claim_limit: number;
	brain_task_claim_limit: number;
	auto_approve_code_edits: boolean;
	auto_approve_promotions: boolean;
	code_strategy_requires_approval: boolean;
	task_stale_recovery_minutes: number;
	health_checks_enabled: boolean;
	rolling_backtest_days: number;
	walkforward_months: number;
	walkforward_folds: number;
	regime_detection_enabled: boolean;
	relaxed_trade_filters_enabled: boolean;
	strict_regime_gating: boolean;
	regime_min_confidence: number;
	allow_unknown_regime_strategies: boolean;
	alert_on_degradation_pct: number;
	backtest_fee_bps: number;
	backtest_slippage_bps: number;
	backtest_timeframe: string;
	backtest_symbol: string;
	backtest_duration_days: number;
	walkforward_cv_method: string;
	walkforward_train_ratio: number;
	walkforward_purge_gap: number;
	walkforward_embargo_pct: number;
	walkforward_objective: string;
	walkforward_n_trials: number;
	remote_engine_enabled: boolean;
	remote_engine_url: string;
	remote_engine_data_root: string;
	research_settings?: ResearchSettings;
	backup_ai_provider?: string;
	backup_ai_model?: string;
	updated_at: string;
}

export interface ApiKeyStatus {
	source: string;
	is_configured: boolean;
	last_tested: string | null;
	test_status: string | null;
}

export interface FactoryResetCategory {
	id: string;
	label: string;
	description: string;
	default_keep: boolean;
}

export async function getSettings(): Promise<ForvenSettings> {
	return fetchApi('/settings');
}

export async function updateSettingsSection(section: string, data: Record<string, unknown>): Promise<{ status: string }> {
	if (section === 'pipeline') {
		return fetchApi(`/settings/pipeline`, {
			method: 'PUT',
			body: JSON.stringify({ updates: data, actor: 'manual' })
		});
	}
	return fetchApi(`/settings/${section}`, {
		method: 'PUT',
		body: JSON.stringify(data)
	});
}

export interface SettingsAuditEntry {
	id: string;
	from: unknown;
	to: unknown;
	at: string;
	actor: string;
}

export async function getSettingsAuditLog(limit = 5): Promise<SettingsAuditEntry[]> {
	return fetchApi<SettingsAuditEntry[]>(`/settings/audit-log?limit=${limit}`);
}

export async function testDiscordNotification(): Promise<{ status: string }> {
	return fetchApi('/settings/test-discord', { method: 'POST' });
}

export async function testRemoteEngine(url: string): Promise<{ ok: boolean; message: string; data?: any }> {
	return fetchApi('/settings/test-remote-engine', {
		method: 'POST',
		body: JSON.stringify({ url })
	});
}

export async function resetSettings(): Promise<{ status: string }> {
	return fetchApi('/settings/reset', { method: 'POST' });
}

export async function getApiKeys(): Promise<ApiKeyStatus[]> {
	return fetchApi('/settings/api-keys');
}

export async function setApiKey(source: string, apiKey: string): Promise<{ status: string }> {
	return fetchApi('/settings/api-keys', {
		method: 'POST',
		body: JSON.stringify({ source, api_key: apiKey })
	});
}

export async function deleteApiKey(source: string): Promise<{ status: string }> {
	return fetchApi(`/settings/api-keys/${source}`, { method: 'DELETE' });
}

export async function testApiKey(source: string): Promise<{ status: string; source: string; tested_at: string }> {
	return fetchApi(`/settings/api-keys/${source}/test`, { method: 'POST' });
}

// ============== System Heartbeat ==============

export type NavIndicatorKind = 'none' | 'count' | 'status' | 'activity';
export type NavIndicatorSeverity = 'neutral' | 'info' | 'success' | 'warn' | 'danger';

export interface SystemNavIndicator {
	kind: NavIndicatorKind;
	severity: NavIndicatorSeverity;
	label: string;
	summary: string;
	count?: number;
	seen_key: string;
}

export interface SystemHeartbeatResponse {
	dashboard: ForvenDashboardResponse;
	risk: ForvenRiskStatus;
	sentiment: ForvenSentimentSnapshot;
	regime: ForvenRegimeSnapshot;
	scanner_state: ForvenScannerState;
	open_trades: ForvenTrade[];
	agent_tasks: Array<Record<string, unknown>>;
	datasets: Array<Record<string, unknown>>;
	research_metrics: { total: number; new_count: number; reviewed_count: number };
	scans: Array<{ id: string; status: string; [key: string]: unknown }>;
	paper_sessions: Array<{ id: string; status: string; [key: string]: unknown }>;
	strategies: Array<Record<string, unknown>>;
	approvals: Array<Record<string, unknown>>;
	nav_indicators?: Record<string, SystemNavIndicator>;
}

export async function getSystemHeartbeat(): Promise<SystemHeartbeatResponse> {
	return fetchApi('/system/heartbeat');
}

export type BackendSoakStatus = 'ok' | 'warn' | 'fail';

export interface BackendSoakSummary {
	execution_mode: string;
	table_counts: Record<string, number>;
	strategy_count: number;
	stage_counts: Record<string, number>;
	open_trades: number;
	paper_sessions: number;
	pending_approvals: number;
	stale_agent_tasks: number;
	stale_brain_tasks: number;
	daemon_running: boolean;
	daemon_age_seconds: number | null;
	scanner_age_seconds: number | null;
	recent_runtime_failures: number;
	reconciliation_issues: number;
	scheduler_job_count: number;
}

export interface BackendSoakCheck {
	name: string;
	status: BackendSoakStatus;
	summary: string;
	details: Record<string, unknown>;
}

export interface BackendSoakReport {
	generated_at: string;
	status: BackendSoakStatus;
	summary: BackendSoakSummary;
	checks: BackendSoakCheck[];
}

export interface BackendSoakReportOptions {
	requireExchangeConnection?: boolean;
	staleTaskMinutes?: number;
}

export async function getBackendSoakReport(
	options: BackendSoakReportOptions = {},
): Promise<BackendSoakReport> {
	const params = new URLSearchParams();
	if (options.requireExchangeConnection !== undefined) {
		params.set('require_exchange_connection', String(options.requireExchangeConnection));
	}
	if (options.staleTaskMinutes !== undefined) {
		params.set('stale_task_minutes', String(options.staleTaskMinutes));
	}
	const query = params.toString();
	return fetchApi(`/system/soak-report${query ? `?${query}` : ''}`);
}

export async function getFactoryResetCategories(): Promise<{ categories: FactoryResetCategory[] }> {
	return fetchApi('/system/factory-reset/categories');
}

export async function performFactoryReset(keep: string[]): Promise<{ status: string; wiped: string[]; kept: string[] }> {
	return fetchApi('/system/factory-reset', {
		method: 'POST',
		body: JSON.stringify({ keep }),
	});
}

// ---------------------------------------------------------------------------
// Health Monitor
// ---------------------------------------------------------------------------

export async function getHealthStatus(): Promise<import('./types').HealthStatusResponse> {
	return fetchApi('/health/status');
}

export async function getHealthAlerts(severity?: string): Promise<import('./types').HealthAlertsResponse> {
	const params = severity ? `?severity=${encodeURIComponent(severity)}` : '';
	return fetchApi(`/health/alerts${params}`);
}
