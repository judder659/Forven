import type { OHLCVBar } from './data';
import {
	fetchApi,
} from './core';

// ============== Paper Trading ==============

export interface PaperPosition {
	id: string;
	symbol: string;
	side: 'long' | 'short' | 'flat';
	entry_price: number;
	entry_time: string;
	size: number;
	current_price: number;
	unrealized_pnl: number;
	unrealized_pnl_pct: number;
	stop_loss_price?: number | null;
	take_profit_price?: number | null;
	stop_loss_source?: string | null;
	take_profit_source?: string | null;
	/** True when the operator paused scanner auto-management for this position. */
	manual_pause?: boolean;
	/** Provenance of the position ("manual" for hand-opened/taken-over trades). */
	source?: string | null;
	/** Direction book (Approach C sub-account) a live position routes to. */
	book?: string | null;
}

export interface PaperNetPosition {
	sides: string[];
	gross_long_size: number;
	gross_short_size: number;
	net_size: number;
	current_price: number;
	unrealized_pnl: number;
	unrealized_pnl_pct: number;
	position_count: number;
}

export interface PaperTrade {
	id: string;
	symbol: string;
	side: string;
	entry_price: number;
	entry_time: string;
	exit_price: number | null;
	exit_time: string | null;
	size: number;
	pnl: number | null;
	pnl_pct: number | null;
	strategy_name: string;
	gross_pnl: number | null;
	fees_paid: number;
	funding_pnl: number;
	net_pnl: number | null;
	net_pnl_pct: number | null;
	entry_fee_bps: number;
	exit_fee_bps: number;
	close_reason?: string | null;
	close_incomplete?: boolean;
}

export interface LiveIndicator {
	name: string;
	value: number;
	timestamp: string;
}

export interface PendingSignal {
	signal_type: 'entry' | 'exit' | 'none';
	indicator_name: string;
	current_value: number;
	trigger_value: number;
	distance_pct: number;
	description: string;
}

export interface ReplayState {
	cursor: number;
	total_bars: number;
	is_playing: boolean;
	progress_pct: number;
}

export interface PaperTradingSession {
	id: string;
	strategy_id?: string;
	strategy_name: string;
	strategy_type?: string | null;
	runtime_type?: string | null;
	runtime_source?: string | null;
	strategy_version: string;
	symbol: string;
	timeframe: string;
	params: Record<string, unknown>;
	default_params?: Record<string, unknown>;
	decision_params?: Record<string, unknown>;
	runtime_diagnostics?: Record<string, unknown> | null;
	mode: 'live' | 'replay';
	live_feed: 'default' | 'ibkr';
	ibkr_sec_type: string;
	ibkr_exchange: string;
	ibkr_currency: string;
	ibkr_what_to_show: 'TRADES' | 'MIDPOINT' | 'BID' | 'ASK';
	replay_start?: string | null;
	replay_end?: string | null;
	replay_speed?: number;
	replay_state?: ReplayState;
	initial_capital: number | null;
	position_size_pct: number;
	stop_loss_pct: number | null;
	take_profit_pct: number | null;
	trailing_stop_pct: number | null;
	leverage: number;
	fee_mode: 'taker' | 'maker' | 'auto';
	taker_fee_bps: number;
	maker_fee_bps: number;
	funding_mode: 'off' | 'fixed' | 'exchange';
	funding_rate_bps_per_interval: number;
	funding_interval_hours: number;
	accrued_funding: number;
	status: 'stopped' | 'watching' | 'position_open' | 'warming_up' | 'replay_finished' | 'gated' | 'blocked';
	current_price: number;
	position: PaperPosition | null;
	positions?: PaperPosition[];
	net_position?: PaperNetPosition | null;
	trade_mode?: 'long_only' | 'short_only' | 'both';
	position_model?: 'single_side' | 'hedged';
	trades: PaperTrade[];
	indicators: Record<string, LiveIndicator>;
	pending_signals: PendingSignal[];
	last_signal: string;
	capital: number | null;
	// Real Hyperliquid balance (deployed/live sessions only). For paper sessions
	// account_value is null and balance_source is 'simulated'. balance_source is
	// 'unavailable' for a deployed session whose real balance hasn't synced yet —
	// the UI must show "balance unavailable", never the simulated $10k base.
	account_value?: number | null;
	account_withdrawable?: number | null;
	account_margin_used?: number | null;
	balance_source?: 'exchange' | 'books_only' | 'books_aggregate' | 'simulated' | 'unavailable' | string | null;
	account_network?: string | null;
	account_synced_at?: string | null;
	// GO-LIVE-1: per-asset notional ceiling (USD) enforced on every live open;
	// null = no per-strategy cap. Deployed/live sessions only.
	live_notional_ceiling_usd?: number | null;
	total_pnl: number;
	total_pnl_pct: number | null;
	total_trades: number;
	winning_trades: number;
	performance?: PaperSessionPerformance;
	win_rate_pct?: number | null;
	avg_pnl?: number | null;
	avg_pnl_pct?: number | null;
	profit_factor?: number | null;
	expectancy?: number | null;
	started_at: string | null;
	compat_kind?: 'paper' | 'deployed';
	gated_by_regime?: boolean;
	gated_reason?: string;
	blocked_reason?: string | null;
}

export interface PaperSessionPerformance {
	closed_trades: number;
	winning_trades: number;
	losing_trades: number;
	win_rate_pct: number;
	gross_profit: number;
	gross_loss: number;
	net_pnl: number;
	avg_pnl: number;
	avg_pnl_pct: number;
	profit_factor: number | null;
	expectancy: number;
	best_trade: number | null;
	worst_trade: number | null;
	last_trade_at: string | null;
}

export interface GetPaperSessionsOptions {
	includeDeployed?: boolean;
	onlyDeployed?: boolean;
	sessionLimit?: number;
	tradesLimit?: number;
}

export interface StartPaperServiceOptions {
	highActivityTest?: boolean;
	runScanNow?: boolean;
}

export interface StopPaperServiceOptions {
	disableTestMode?: boolean;
}

export interface PaperServiceStatus {
	status: string;
	running?: boolean;
	high_activity_test?: boolean;
	scanner_jobs_enabled?: boolean;
	scan_triggered?: boolean;
	scan_error?: string | null;
}

function unsupportedPaperControl(name: string): never {
	throw new Error(`${name} is not supported by the current compatibility paper service`);
}

// Create a new paper trading session
export async function createPaperSession(
	strategyName: string,
	symbol: string,
	timeframe: string = '1h',
	params?: Record<string, unknown>,
	initialCapital: number = 10000,
	positionSizePct: number = 100,
	mode: 'live' | 'replay' = 'live',
	replayStart?: string,
	replayEnd?: string,
	replaySpeed: number = 1,
	liveFeed: 'default' | 'ibkr' = 'default',
	ibkrSecType: string = 'STK',
	ibkrExchange: string = 'SMART',
	ibkrCurrency: string = 'USD',
	ibkrWhatToShow: 'TRADES' | 'MIDPOINT' | 'BID' | 'ASK' = 'TRADES',
	feeMode: 'taker' | 'maker' | 'auto' = 'taker',
	takerFeeBps: number = 4.5,
	makerFeeBps: number = 1.5,
	fundingMode: 'off' | 'fixed' | 'exchange' = 'off',
	fundingRateBpsPerInterval: number = 0,
	fundingIntervalHours: number = 8
): Promise<PaperTradingSession> {
	return unsupportedPaperControl('createPaperSession');
}

// Update a paper trading session
export async function updatePaperSession(
	sessionId: string,
	updates: Partial<{
		strategy_name: string;
		symbol: string;
		timeframe: string;
		params: Record<string, unknown>;
		initial_capital: number;
		position_size_pct: number;
		stop_loss_pct: number | null;
		take_profit_pct: number | null;
		trailing_stop_pct: number | null;
		mode: 'live' | 'replay';
		live_feed: 'default' | 'ibkr';
		ibkr_sec_type: string;
		ibkr_exchange: string;
		ibkr_currency: string;
		ibkr_what_to_show: 'TRADES' | 'MIDPOINT' | 'BID' | 'ASK';
		replay_start: string | null;
		replay_end: string | null;
		replay_speed: number;
		fee_mode: 'taker' | 'maker' | 'auto';
		taker_fee_bps: number;
		maker_fee_bps: number;
		funding_mode: 'off' | 'fixed' | 'exchange';
		funding_rate_bps_per_interval: number;
		funding_interval_hours: number;
	}>
): Promise<PaperTradingSession> {
	return unsupportedPaperControl('updatePaperSession');
}

// Get all paper trading sessions
export async function getPaperSessions(options: GetPaperSessionsOptions = {}): Promise<PaperTradingSession[]> {
	const params = new URLSearchParams();
	if (options.includeDeployed) {
		params.set('include_deployed', 'true');
	}
	if (options.onlyDeployed) {
		params.set('only_deployed', 'true');
	}
	if (typeof options.sessionLimit === 'number' && Number.isFinite(options.sessionLimit) && options.sessionLimit > 0) {
		params.set('session_limit', String(Math.floor(options.sessionLimit)));
	}
	if (typeof options.tradesLimit === 'number' && Number.isFinite(options.tradesLimit) && options.tradesLimit > 0) {
		params.set('trades_limit', String(Math.floor(options.tradesLimit)));
	}
	const suffix = params.toString();
	return fetchApi(`/paper/sessions${suffix ? `?${suffix}` : ''}`);
}

// Get a specific paper trading session
export async function getPaperSession(sessionId: string): Promise<PaperTradingSession> {
	return fetchApi(`/paper/sessions/${sessionId}`);
}

// Start a paper trading session
export async function startPaperSession(sessionId: string): Promise<PaperTradingSession> {
	return unsupportedPaperControl('startPaperSession');
}

// Stop a paper trading session
export async function stopPaperSession(sessionId: string): Promise<PaperTradingSession> {
	return unsupportedPaperControl('stopPaperSession');
}

// Delete a paper trading session
export async function deletePaperSession(sessionId: string): Promise<{ status: string }> {
	return unsupportedPaperControl('deletePaperSession');
}

// ============== Manual Position Controls (paper) ==============
// Each posts a light mutation and returns the refreshed session so the UI can
// update in one round-trip. Backend enforces operator auth + paper-only.

export interface OpenManualPaperPositionOptions {
	direction: 'long' | 'short';
	size?: number;
	riskPct?: number;
	leverage?: number;
	stopLossPrice?: number | null;
	takeProfitPrice?: number | null;
}

// Close the current position at the paper mid
export async function closePaperPosition(
	sessionId: string,
	reason?: string
): Promise<PaperTradingSession> {
	return fetchApi(`/paper/sessions/${sessionId}/close-position`, {
		method: 'POST',
		body: JSON.stringify({ reason: reason ?? null }),
	});
}

// Close part of the position (by quantity or percent); residual stays open
export async function partialClosePaperPosition(
	sessionId: string,
	args: { qty?: number; pct?: number }
): Promise<PaperTradingSession> {
	return fetchApi(`/paper/sessions/${sessionId}/partial-close`, {
		method: 'POST',
		body: JSON.stringify({ qty: args.qty ?? null, pct: args.pct ?? null }),
	});
}

// Open a brand-new position by hand (one per strategy/asset)
export async function openManualPaperPosition(
	sessionId: string,
	options: OpenManualPaperPositionOptions
): Promise<PaperTradingSession> {
	return fetchApi(`/paper/sessions/${sessionId}/open-position`, {
		method: 'POST',
		body: JSON.stringify({
			direction: options.direction,
			size: options.size ?? null,
			risk_pct: options.riskPct ?? null,
			leverage: options.leverage ?? 1,
			stop_loss_price: options.stopLossPrice ?? null,
			take_profit_price: options.takeProfitPrice ?? null,
		}),
	});
}

// Set/clear (price=null) the absolute stop-loss on the open position
export async function adjustPaperStopLoss(
	sessionId: string,
	price: number | null
): Promise<PaperTradingSession> {
	return fetchApi(`/paper/sessions/${sessionId}/position/stop-loss`, {
		method: 'POST',
		body: JSON.stringify({ price }),
	});
}

// Set/clear (price=null) the absolute take-profit on the open position
export async function adjustPaperTakeProfit(
	sessionId: string,
	price: number | null
): Promise<PaperTradingSession> {
	return fetchApi(`/paper/sessions/${sessionId}/position/take-profit`, {
		method: 'POST',
		body: JSON.stringify({ price }),
	});
}

// Close the position and re-open the opposite side at the same size
export async function flipPaperPosition(sessionId: string): Promise<PaperTradingSession> {
	return fetchApi(`/paper/sessions/${sessionId}/flip`, { method: 'POST' });
}

// Pause/resume scanner auto-management for the open position (full detach)
export async function setPaperAutoManagement(
	sessionId: string,
	paused: boolean
): Promise<PaperTradingSession> {
	return fetchApi(`/paper/sessions/${sessionId}/position/auto-management`, {
		method: 'POST',
		body: JSON.stringify({ paused }),
	});
}

// ============== Replay Controls ==============

export interface ReplayStepResult {
	stepped: number;
	cursor: number;
	total: number;
	is_complete: boolean;
	bars?: OHLCVBar[];
}

export interface ReplaySeekResult {
	cursor: number;
	total: number;
	bars?: OHLCVBar[];
}

export interface ReplayPlayResult {
	is_playing: boolean;
	speed: number;
}

export interface ReplayPauseResult {
	is_playing: boolean;
	cursor: number;
}

export interface ReplaySpeedResult {
	speed: number;
}

export interface ReplayResetResult {
	cursor: number;
	status: string;
}

// Step forward in replay mode
export async function replayStep(sessionId: string, count: number = 1): Promise<ReplayStepResult> {
	return unsupportedPaperControl('replayStep');
}

// Seek to specific bar index
export async function replaySeek(sessionId: string, index: number): Promise<ReplaySeekResult> {
	return unsupportedPaperControl('replaySeek');
}

// Start/resume auto-play
export async function replayPlay(sessionId: string, speed?: number): Promise<ReplayPlayResult> {
	return unsupportedPaperControl('replayPlay');
}

// Pause auto-play
export async function replayPause(sessionId: string): Promise<ReplayPauseResult> {
	return unsupportedPaperControl('replayPause');
}

// Set playback speed
export async function replaySetSpeed(sessionId: string, speed: number): Promise<ReplaySpeedResult> {
	return unsupportedPaperControl('replaySetSpeed');
}

// Reset replay to beginning
export async function replayReset(sessionId: string): Promise<ReplayResetResult> {
	return unsupportedPaperControl('replayReset');
}

// Get session chart bars, optionally overriding the timeframe for live chart inspection
export async function getReplayBars(
	sessionId: string,
	limit: number = 500,
	timeframe?: string
): Promise<OHLCVBar[]> {
	const params = new URLSearchParams();
	params.set('limit', String(limit));
	if (timeframe) {
		params.set('timeframe', timeframe);
	}
	return fetchApi(`/paper/sessions/${sessionId}/replay/bars?${params.toString()}`);
}

// Get trade history for a session
export async function getPaperTrades(sessionId: string, limit: number = 50): Promise<PaperTrade[]> {
	return fetchApi(`/paper/sessions/${sessionId}/trades?limit=${limit}`);
}

// Start paper trading service
export async function startPaperService(options: StartPaperServiceOptions = {}): Promise<PaperServiceStatus> {
	const params = new URLSearchParams();
	if (typeof options.highActivityTest === 'boolean') {
		params.set('high_activity_test', String(options.highActivityTest));
	}
	if (typeof options.runScanNow === 'boolean') {
		params.set('run_scan_now', String(options.runScanNow));
	}
	const suffix = params.toString();
	return fetchApi(`/paper/service/start${suffix ? `?${suffix}` : ''}`, { method: 'POST' });
}

// Stop paper trading service
export async function stopPaperService(options: StopPaperServiceOptions = {}): Promise<PaperServiceStatus> {
	const params = new URLSearchParams();
	if (typeof options.disableTestMode === 'boolean') {
		params.set('disable_test_mode', String(options.disableTestMode));
	}
	const suffix = params.toString();
	return fetchApi(`/paper/service/stop${suffix ? `?${suffix}` : ''}`, { method: 'POST' });
}

// WebSocket URL for paper trading updates
export function getPaperWebSocketUrl(sessionId: string): string {
	return unsupportedPaperControl('getPaperWebSocketUrl');
}

// ============== Session Indicators & Markers ==============

export interface IndicatorHistoryPoint {
	timestamp: string;
	value: number | null;
}

export interface SessionIndicatorConfig {
	panel: 'main' | 'sub' | 'none';
	type: 'line' | 'histogram' | 'area' | 'scatter';
	color?: string;
	style?: 'solid' | 'dashed' | 'dotted';
	linewidth?: number;
}

export interface SessionIndicatorsResponse {
	session_id: string;
	config: Record<string, SessionIndicatorConfig>;
	indicators: Record<string, IndicatorHistoryPoint[]>;
}

export interface TradeMarker {
	timestamp: string;
	price: number;
	trade_id: string;
	direction?: 'long' | 'short' | string;
	marker_kind?: 'trade' | 'signal' | string;
	reason?: string;
	pnl?: number;
	pnl_pct?: number;
	is_open?: boolean;
	// Self-describing visuals from the backend (industry-standard marker conventions).
	side?: 'bull' | 'bear' | string;
	action?: 'buy' | 'sell' | 'short' | 'cover' | 'long_signal' | 'short_signal' | 'exit_signal' | string;
	shape?: 'arrowUp' | 'arrowDown' | string;
	color?: string;
	label?: string;
}

export interface TradeMarkersResponse {
	entries: TradeMarker[];
	exits: TradeMarker[];
	blocked?: TradeMarker[];
}

export interface TradeMarkersOptions {
	limit?: number;
	includeGenerated?: boolean;
	timeoutMs?: number;
}

// Get indicator history for chart visualization
export async function getSessionIndicators(
	sessionId: string,
	indicatorNames?: string[],
	limit: number = 500,
	timeframe?: string
): Promise<SessionIndicatorsResponse> {
	const params = new URLSearchParams();
	if (indicatorNames && indicatorNames.length > 0) {
		params.set('indicators', indicatorNames.join(','));
	}
	params.set('limit', limit.toString());
	if (timeframe) {
		params.set('timeframe', timeframe);
	}
	return fetchApi(`/paper/sessions/${sessionId}/indicators?${params}`);
}

// Get trade entry/exit markers for chart visualization
export async function getTradeMarkers(
	sessionId: string,
	options: TradeMarkersOptions = {}
): Promise<TradeMarkersResponse> {
	const params = new URLSearchParams();
	if (options.limit !== undefined) {
		params.set('limit', options.limit.toString());
	}
	if (options.includeGenerated !== undefined) {
		params.set('include_generated', options.includeGenerated ? 'true' : 'false');
	}
	const query = params.toString();
	return fetchApi(
		`/paper/sessions/${sessionId}/markers${query ? `?${query}` : ''}`,
		options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : undefined
	);
}

// ============== Chart bundle (parity overhaul) ==============
// One call returns everything the chart needs, driven by the real indicator
// registry + the strategy's own signal function (no guessed reimplementation).

export interface ChartIndicatorSeries {
	name: string;
	panel: 'main' | 'sub';
	type: 'line';
	color?: string;
	data: IndicatorHistoryPoint[];
}

export interface ActiveLevel {
	price: number;
	direction?: string;
	from_time?: string;
	to_time?: string | null;
	type?: 'stop' | 'take_profit' | 'trail' | 'entry' | string;
	label?: string;
	color?: string;
}

export interface PaperSessionChart {
	session_id: string;
	bars: OHLCVBar[];
	main_indicators: ChartIndicatorSeries[];
	sub_indicators: ChartIndicatorSeries[];
	entry_markers: TradeMarker[];
	exit_markers: TradeMarker[];
	trigger_entries: TradeMarker[];
	trigger_exits: TradeMarker[];
	active_levels: { stop: ActiveLevel[]; take_profit: ActiveLevel[]; trail: ActiveLevel[]; entry?: ActiveLevel[] };
	strategy_type: string;
	warnings: string[];
}

export async function getPaperSessionChart(
	sessionId: string,
	options: { limit?: number; timeframe?: string; timeoutMs?: number } = {}
): Promise<PaperSessionChart> {
	const params = new URLSearchParams();
	params.set('limit', String(options.limit ?? 2000));
	if (options.timeframe) params.set('timeframe', options.timeframe);
	return fetchApi(
		`/paper/sessions/${sessionId}/chart?${params.toString()}`,
		options.timeoutMs !== undefined ? { timeoutMs: options.timeoutMs } : undefined
	);
}
