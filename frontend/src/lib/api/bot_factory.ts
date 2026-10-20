import { fetchApi } from './core';

// ── Types ──────────────────────────────────────────────────────────

export type BotDecisionCadence = 'candle_close' | 'interval';
export type BotTimeframe = '15m' | '1h' | '4h' | '1d';

export interface BotModelCheck {
	ok: boolean;
	provider: string | null;
	model_id: string | null;
	reason: string | null;
	/** The operator's primary model as "provider:model", offered as the fix. */
	default_model: string | null;
}

export interface BotSessionHours {
	timezone: string;
	days: string[];
	start: string;
	end: string;
}

export type BotInput =
	| 'indicators'
	| 'higher_timeframe'
	| 'funding'
	| 'open_interest'
	| 'regime'
	| 'strategy_signals';

export type BotType = 'ai' | 'grid' | 'dca' | 'signal';

export interface BotFollowableStrategy {
	id: string;
	name: string;
	symbol: string | null;
	timeframe: string | null;
	stage: 'paper' | 'live';
	open_trades: number;
}

export async function listFollowableStrategies(): Promise<BotFollowableStrategy[]> {
	return fetchApi('/bot-factory/followable-strategies');
}

export interface BotConfig {
	id: string;
	name: string;
	model: string;
	soul: string | null;
	context: string | null;
	strategy: string | null;
	guardrails: string | null;
	capital_allocation: number;
	max_position_pct: number;
	max_concurrent_positions: number;
	max_drawdown_pct: number;
	stop_loss_pct: number | null;
	take_profit_pct: number | null;
	taker_fee_bps: number;
	slippage_bps: number;
	cooldown_seconds: number;
	session_hours: BotSessionHours | null;
	reasoning_verbosity: string;
	asset_mode: string;
	locked_pairs: string[] | null;
	max_llm_calls_per_day: number;
	max_consecutive_errors: number;
	/** Daily AI spend cap in USD (estimated); null = only the call cap. Send 0 to clear. */
	max_llm_cost_per_day: number | null;
	/** candle_close = decide once per new 1h candle; interval = every cooldown_seconds. */
	decision_cadence: BotDecisionCadence;
	/** Candle timeframe the bot reads and, on candle_close, decides on. */
	timeframe: BotTimeframe;
	/** Extra inputs joined to the candles in the prompt; null/empty = candles only. */
	data_sources: BotInput[] | null;
	/** ai asks a model; grid, dca and signal decide in code from rule_params. */
	bot_type: BotType;
	rule_params: Record<string, unknown> | null;
	template_id: string | null;
	execution_mode: 'paper' | 'live';
	live_wallet: string | null;
	status: string;
	created_at: string;
	updated_at: string;
	// Joined from bot_status
	pid: number | null;
	runtime_status: string | null;
	last_heartbeat: string | null;
	started_at: string | null;
	error_message: string | null;
	llm_calls_today: number | null;
	consecutive_errors: number | null;
	/** Run that owns the bot right now (null when stopped). */
	run_id: string | null;
	/** Estimated AI spend today, USD. */
	llm_cost_today: number | null;
	/** Whether the bot's model can be called on this install. */
	model_check?: BotModelCheck;
	// List-endpoint trade digest (absent on getBot)
	realized_pnl?: number;
	open_positions?: number;
	closed_trades?: number;
	// Real balance of the routed wallet for LIVE bots (list endpoint only).
	live_wallet_equity?: number | null;
}

export interface BotDecision {
	id: number;
	bot_id: string;
	timestamp: string;
	event_trigger: Record<string, unknown> | null;
	reasoning: string | null;
	action_type: string;
	action_data: Record<string, unknown> | null;
	verbosity_level: string;
	/** Run that made the decision (two copies can never interleave unseen). */
	run_id: string | null;
	/** Estimated AI cost of this decision, USD (null when not an AI call). */
	cost_usd: number | null;
	tokens_in: number | null;
	tokens_out: number | null;
	/** True when the prompt, reply and risk check were kept for the inspector. */
	has_details?: boolean;
}

export interface BotPromptMessage {
	role: string;
	content: string;
}

/** One decision with what the bot was shown and answered (the inspector). */
export interface BotDecisionDetail extends BotDecision {
	details: {
		model: string | null;
		prompt: BotPromptMessage[] | null;
		response: string | null;
		parsed: Record<string, unknown> | null;
		risk_check: { allowed: boolean; reason: string } | null;
		created_at: string | null;
	} | null;
}

export interface BotEquityPoint {
	timestamp: string;
	equity: number;
}

export interface BotPerformanceSummary {
	starting_capital: number;
	equity: number;
	pnl_usd: number;
	return_pct: number | null;
	benchmark_symbol: string;
	benchmark_return_pct: number | null;
	excess_return_pct: number | null;
	sharpe: number | null;
	max_drawdown_pct: number;
	closed_trades: number;
	win_rate: number | null;
	profit_factor: number | null;
	fees_usd: number;
	fees_per_trade_usd: number | null;
	ai_cost_usd: number;
	pnl_after_ai_usd: number;
	ai_cost_pct_of_pnl: number | null;
	start_at: string;
	end_at: string;
	open_positions: number;
	funding_usd: number;
}

export interface BotPerformance {
	bot_id: string;
	summary: BotPerformanceSummary;
	curve: BotEquityPoint[];
	benchmark_curve: BotEquityPoint[] | null;
}

export interface BotChartMarker {
	timestamp: string;
	price: number;
	type: 'entry' | 'exit';
	direction: string | null;
	pnl_usd?: number | null;
}

export interface BotChart {
	bot_id: string;
	pair: string;
	pairs: string[];
	timeframe: BotTimeframe;
	candles: Array<{ timestamp: string; open: number; high: number; low: number; close: number; volume: number }>;
	entries: BotChartMarker[];
	exits: BotChartMarker[];
}

export type BotHealthLevel = 'ok' | 'warn' | 'bad' | 'idle';

export interface BotFleetRow {
	bot_id: string;
	sparkline: number[];
	equity: number;
	return_pct: number | null;
	benchmark_return_pct: number | null;
	excess_return_pct: number | null;
	win_rate: number | null;
	closed_trades: number;
	open_positions: number;
	ai_cost_today_usd: number;
	ai_cost_usd: number;
	start_at: string;
	health: {
		level: BotHealthLevel;
		reasons: string[];
		last_decision_at: string | null;
		last_action_type: string | null;
	};
}

export interface BotFleet {
	benchmark_symbol: string;
	bots: BotFleetRow[];
}

export interface BotConfigVersion {
	id: number;
	bot_id: string;
	version: number;
	config_snapshot: Record<string, unknown>;
	created_at: string;
}

export interface BotTemplate {
	id: string;
	name: string;
	description: string | null;
	is_builtin: number;
	config_snapshot: Record<string, unknown>;
	created_at: string;
}

export interface BotVersionDiff {
	v1: number;
	v2: number;
	changes: Record<string, { v1: unknown; v2: unknown }>;
}

// ── Bot CRUD ───────────────────────────────────────────────────────

export async function listBots(): Promise<BotConfig[]> {
	return fetchApi('/bot-factory/bots');
}

export async function getBot(id: string): Promise<BotConfig> {
	return fetchApi(`/bot-factory/bots/${id}`);
}

export async function createBot(config: Partial<BotConfig>): Promise<BotConfig> {
	return fetchApi('/bot-factory/bots', {
		method: 'POST',
		body: JSON.stringify(config),
	});
}

export async function updateBot(id: string, updates: Partial<BotConfig>): Promise<BotConfig> {
	return fetchApi(`/bot-factory/bots/${id}`, {
		method: 'PUT',
		body: JSON.stringify(updates),
	});
}

export async function deleteBot(id: string): Promise<{ status: string }> {
	return fetchApi(`/bot-factory/bots/${id}`, { method: 'DELETE' });
}

// ── Bot Lifecycle ──────────────────────────────────────────────────

export async function startBot(id: string): Promise<{ status: string; pid: number; run_id?: string }> {
	return fetchApi(`/bot-factory/bots/${id}/start`, { method: 'POST' });
}

/** Switch a bot to the operator's default model (the fix when Start refuses a retired model). */
export async function useDefaultModel(id: string): Promise<BotConfig> {
	return fetchApi(`/bot-factory/bots/${id}/use-default-model`, { method: 'POST' });
}

export interface BotCostEstimate {
	provider: string | null;
	model_id: string | null;
	cadence: BotDecisionCadence;
	timeframe: BotTimeframe;
	decisions_wanted_per_day: number;
	calls_per_day: number;
	/** Hours of trading before the daily call cap pauses the bot (null = never hits it). */
	hours_until_call_cap: number | null;
	hours_until_spend_cap: number | null;
	tokens_in_per_call: number;
	tokens_out_per_call: number;
	usd_per_call: number | null;
	usd_per_day: number | null;
	/** False when the model has no price row and a provider ceiling rate was used. */
	priced: boolean;
	/** True when tokens per call come from the bot's own recent decisions. */
	from_history: boolean;
	model_check: BotModelCheck;
}

export interface BotCostEstimateParams {
	model: string;
	decision_cadence: BotDecisionCadence;
	timeframe: BotTimeframe;
	cooldown_seconds: number;
	asset_mode: string;
	locked_pairs: string[];
	reasoning_verbosity: string;
	max_llm_calls_per_day: number;
	max_llm_cost_per_day: number | null;
	bot_id?: string | null;
	data_sources?: BotInput[];
}

/** Projected AI calls and spend per day for an editor configuration (estimates). */
export async function getBotCostEstimate(params: BotCostEstimateParams): Promise<BotCostEstimate> {
	const q = new URLSearchParams({
		decision_cadence: params.decision_cadence,
		timeframe: params.timeframe,
		cooldown_seconds: String(params.cooldown_seconds),
		asset_mode: params.asset_mode,
		reasoning_verbosity: params.reasoning_verbosity,
		max_llm_calls_per_day: String(params.max_llm_calls_per_day),
	});
	if (params.model) q.set('model', params.model);
	if (params.locked_pairs.length) q.set('locked_pairs', params.locked_pairs.join(','));
	if (params.max_llm_cost_per_day) q.set('max_llm_cost_per_day', String(params.max_llm_cost_per_day));
	if (params.bot_id) q.set('bot_id', params.bot_id);
	if (params.data_sources?.length) q.set('data_sources', params.data_sources.join(','));
	return fetchApi(`/bot-factory/cost-estimate?${q.toString()}`);
}

export interface BotFlattenResult {
	state: 'closed' | 'pending' | 'failed' | 'noop';
	trade_id?: string;
	fill_price?: number;
	net_pnl?: number;
	message?: string;
}

/** Stopping a LIVE bot flattens its open live positions (reduce-only closes);
 * `flattened` reports the outcome per position. Paper bots return []. */
export async function stopBot(id: string): Promise<{ status: string; flattened?: BotFlattenResult[] }> {
	return fetchApi(`/bot-factory/bots/${id}/stop`, { method: 'POST' });
}

export async function cloneBot(id: string, newName: string): Promise<BotConfig> {
	return fetchApi(`/bot-factory/bots/${id}/clone`, {
		method: 'POST',
		body: JSON.stringify({ new_name: newName }),
	});
}

export async function killAllBots(): Promise<{ stopped: number }> {
	return fetchApi('/bot-factory/kill-all', { method: 'POST' });
}

// ── Execution mode (paper / live) ──────────────────────────────────

/** Arm a bot for LIVE execution. `confirm` must be the typed phrase "GO LIVE";
 * the ceiling bounds every live order's notional (server-enforced GO-LIVE-1).
 * `wallet` optionally routes all live orders to a registered named sub-account
 * wallet (capital isolation); null = master / direction books. */
export async function goLiveBot(
	id: string,
	confirm: string,
	liveNotionalCeilingUsd: number,
	wallet: string | null = null
): Promise<BotConfig> {
	return fetchApi(`/bot-factory/bots/${id}/go-live`, {
		method: 'POST',
		body: JSON.stringify({
			confirm,
			live_notional_ceiling_usd: liveNotionalCeilingUsd,
			wallet,
		}),
	});
}

/** Disarm live execution and return the bot to paper mode. */
export async function goPaperBot(id: string): Promise<BotConfig> {
	return fetchApi(`/bot-factory/bots/${id}/go-paper`, { method: 'POST' });
}

/** Set the wallet the bot's live orders will route to (null = master/books).
 * Only allowed while the bot is in paper mode — a live-armed bot's routing
 * changes go through GO LIVE re-arming. */
export async function setBotWallet(id: string, wallet: string | null): Promise<BotConfig> {
	return fetchApi(`/bot-factory/bots/${id}/wallet`, {
		method: 'POST',
		body: JSON.stringify({ wallet }),
	});
}

// ── Bot Data ───────────────────────────────────────────────────────

export interface BotTrade {
	id: string;
	strategy_name: string | null;
	asset: string;
	symbol: string;
	direction: string;
	size: number;
	entry_price: number;
	exit_price: number | null;
	status: string;
	pnl: number | null;
	pnl_pct: number | null;
	opened_at: string;
	closed_at: string | null;
	source: string;
	signal_data: Record<string, unknown> | null;
}

export async function getBotTrades(id: string, limit = 50): Promise<BotTrade[]> {
	return fetchApi(`/bot-factory/bots/${id}/trades?limit=${limit}`);
}

export interface BotStats {
	bot_id: string;
	total: number;
	open_count: number;
	closed_count: number;
	wins: number;
	losses: number;
	win_rate: number; // 0..1 float
	total_pnl_usd: number;
	best_pnl_usd: number;
	worst_pnl_usd: number;
	/** Estimated AI spend over all decisions, USD. */
	ai_cost_usd?: number;
	ai_priced_calls?: number;
}

export async function getBotStats(id: string): Promise<BotStats> {
	return fetchApi(`/bot-factory/bots/${id}/stats`);
}

export async function getBotDecisions(id: string, limit = 50): Promise<BotDecision[]> {
	return fetchApi(`/bot-factory/bots/${id}/decisions?limit=${limit}`);
}

export async function getBotDecision(id: string, decisionId: number): Promise<BotDecisionDetail> {
	return fetchApi(`/bot-factory/bots/${id}/decisions/${decisionId}`);
}

export async function getBotPerformance(id: string): Promise<BotPerformance> {
	return fetchApi(`/bot-factory/bots/${id}/performance`);
}

export async function getBotChart(id: string, pair?: string | null): Promise<BotChart> {
	const qs = pair ? `?pair=${encodeURIComponent(pair)}` : '';
	return fetchApi(`/bot-factory/bots/${id}/chart${qs}`);
}

export async function getBotFleet(): Promise<BotFleet> {
	return fetchApi('/bot-factory/fleet');
}

/** One decision on live data with the editor's settings; nothing is traded or logged. */
export interface BotTestDecision {
	ran: boolean;
	reason?: string;
	pairs?: string[];
	stale_pairs: Record<string, string>;
	open_positions?: number;
	model?: string;
	prompt?: BotPromptMessage[];
	response?: string;
	parsed?: Record<string, unknown>;
	action?: string;
	risk_check?: { allowed: boolean; reason: string };
	cost_usd?: number | null;
	tokens_in?: number | null;
	tokens_out?: number | null;
	priced?: boolean;
}

export async function testBotDecision(
	config: Partial<BotConfig>,
	botId?: string | null
): Promise<BotTestDecision> {
	return fetchApi('/bot-factory/test-decision', {
		method: 'POST',
		body: JSON.stringify({ bot_id: botId || null, config })
	});
}

// ── Replays ───────────────────────────────────────────────────────

export interface BotReplayTrade {
	trade_id: string;
	ticker: string;
	direction: string;
	qty: number;
	entry_price: number;
	exit_price: number;
	opened_at: string;
	closed_at: string;
	pnl_usd: number;
	fees_usd: number;
	exit_reason: string;
	reasoning: string;
}

export interface BotReplaySummary extends Omit<BotPerformanceSummary, 'start_at' | 'end_at' | 'open_positions' | 'funding_usd'> {
	decisions: number;
	ai_calls: number;
	ai_cached: number;
	refused: number;
	fee_bps: number;
	slippage_bps: number;
	ended_early: string | null;
	pairs?: string[];
	missing_pairs?: string[];
	timeframe?: string;
	inputs_not_replayed?: string[];
	note?: string;
}

export interface BotReplay {
	id: string;
	bot_id: string | null;
	template_id: string | null;
	arena_id: string | null;
	status: 'running' | 'done' | 'failed';
	error: string | null;
	window_start: string;
	window_end: string;
	max_ai_usd: number | null;
	summary: BotReplaySummary | null;
	curve?: BotEquityPoint[] | null;
	benchmark?: BotEquityPoint[] | null;
	trades?: BotReplayTrade[] | null;
	created_at: string;
	finished_at: string | null;
}

export interface BotReplayStarted {
	id: string;
	status: 'running';
	window_start: string;
	window_end: string;
	shifted_for_holdout: boolean;
	max_ai_usd: number | null;
}

export interface BotReplayRequest {
	days?: number;
	end?: string | null;
	/** Dollar cap the operator approved for an AI replay. */
	max_ai_usd?: number | null;
	config?: Partial<BotConfig>;
}

export async function startBotReplay(id: string, body: BotReplayRequest): Promise<BotReplayStarted> {
	return fetchApi(`/bot-factory/bots/${id}/replays`, { method: 'POST', body: JSON.stringify(body) });
}

export async function listBotReplays(id: string, limit = 20): Promise<BotReplay[]> {
	return fetchApi(`/bot-factory/bots/${id}/replays?limit=${limit}`);
}

export async function getBotReplay(replayId: string): Promise<BotReplay> {
	return fetchApi(`/bot-factory/replays/${replayId}`);
}

/** Move an existing bot onto the recommended settings (decide on each new candle). */
export async function applyRecommendedBotSettings(id: string): Promise<BotConfig> {
	return fetchApi(`/bot-factory/bots/${id}/apply-recommended`, { method: 'POST' });
}

export async function getBotVersions(id: string): Promise<BotConfigVersion[]> {
	return fetchApi(`/bot-factory/bots/${id}/versions`);
}

export interface BotMemoryEntry {
	id: string;
	text: string;
	metadata: {
		bot_id?: string;
		timestamp?: string;
		type?: string;
		ticker?: string;
		entry_price?: number;
		exit_price?: number;
		qty?: number;
		pnl?: number;
		pnl_pct?: number;
		outcome?: 'win' | 'loss' | 'flat';
		[key: string]: unknown;
	};
}

export async function getBotMemory(id: string, limit = 50): Promise<BotMemoryEntry[]> {
	return fetchApi(`/bot-factory/bots/${id}/memory?limit=${limit}`);
}

export interface BotOpenPosition {
	trade_id: string;
	ticker: string;
	asset: string | null;
	direction: 'long' | 'short';
	qty: number;
	entry_price: number;
	current_price: number | null;
	unrealized_pnl?: number;
	stop_loss_price: number | null;
	take_profit_price: number | null;
	entry_fee_usd: number;
	opened_at: string;
	execution_type: 'paper' | 'live';
}

export interface BotPositionsSnapshot {
	bot_id: string;
	starting_capital: number;
	realized_pnl: number;
	unrealized_pnl: number;
	equity: number;
	peak_equity: number | null;
	equity_state_started_at: string | null;
	mark_age_seconds: number | null;
	open_positions: BotOpenPosition[];
	execution_mode: 'paper' | 'live';
	live_wallet: string | null;
	live_wallet_equity: number | null;
}

export async function getBotPositions(id: string): Promise<BotPositionsSnapshot> {
	return fetchApi(`/bot-factory/bots/${id}/positions`);
}

export async function diffBotVersions(id: string, v1: number, v2: number): Promise<BotVersionDiff> {
	return fetchApi(`/bot-factory/bots/${id}/versions/${v1}/diff/${v2}`);
}

export interface BotClosePositionResult {
	status: string;
	trade_id: string;
	fill_price?: number;
	net_pnl?: number;
	realized?: number;
}

/** Manually close one of a bot's open positions (server dispatches paper vs live). */
export async function closeBotPosition(
	botId: string,
	tradeId: string,
	reason?: string
): Promise<BotClosePositionResult> {
	return fetchApi(`/bot-factory/bots/${botId}/positions/${tradeId}/close`, {
		method: 'POST',
		body: JSON.stringify({ reason: reason ?? null }),
	});
}

// ── Strategy Bridge ────────────────────────────────────────────────

export interface BotStrategyBridge {
	config: Partial<BotConfig>;
	strategy_id: string;
}

export async function createBotFromStrategy(strategyId: string): Promise<BotStrategyBridge> {
	return fetchApi(`/bot-factory/from-strategy/${strategyId}`);
}

// ── Templates ──────────────────────────────────────────────────────

export async function listTemplates(): Promise<BotTemplate[]> {
	return fetchApi('/bot-factory/templates');
}

export async function getTemplate(id: string): Promise<BotTemplate> {
	return fetchApi(`/bot-factory/templates/${id}`);
}

export async function createTemplate(name: string, description: string | null, config: Record<string, unknown>): Promise<BotTemplate> {
	return fetchApi('/bot-factory/templates', {
		method: 'POST',
		body: JSON.stringify({ name, description, config }),
	});
}

export async function deleteTemplate(id: string): Promise<{ status: string }> {
	return fetchApi(`/bot-factory/templates/${id}`, { method: 'DELETE' });
}
