import { fetchApi } from './core';

// ── Types ──────────────────────────────────────────────────────────

export interface BotSessionHours {
	timezone: string;
	days: string[];
	start: string;
	end: string;
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
	// List-endpoint trade digest (absent on getBot)
	realized_pnl?: number;
	open_positions?: number;
	closed_trades?: number;
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

export async function startBot(id: string): Promise<{ status: string; pid: number }> {
	return fetchApi(`/bot-factory/bots/${id}/start`, { method: 'POST' });
}

export async function stopBot(id: string): Promise<{ status: string }> {
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
}

export async function getBotStats(id: string): Promise<BotStats> {
	return fetchApi(`/bot-factory/bots/${id}/stats`);
}

export async function getBotDecisions(id: string, limit = 50): Promise<BotDecision[]> {
	return fetchApi(`/bot-factory/bots/${id}/decisions?limit=${limit}`);
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
