/**
 * Trading desk API: the live and paper desks read the same endpoints, keyed by mode.
 *
 * - Fleet scorecard: GET /api/dashboard/{live,paper}-fleet
 * - Decision journal: GET /api/trading/journal
 * - Compact fills: GET /api/trading/fills
 * - Funding and open interest: GET /api/trading/market-context
 */
import { fetchApi } from './core';
import type { LiveFleet } from './dashboard';

export type DeskMode = 'live' | 'paper';

export async function getFleet(mode: DeskMode): Promise<LiveFleet> {
	return fetchApi(mode === 'live' ? '/dashboard/live-fleet' : '/dashboard/paper-fleet');
}

// ---- Decision journal ----

export type JournalKind = 'opened' | 'closed' | 'failed' | 'entry_refused' | 'exit_refused' | 'regime_flag';

export interface JournalEvent {
	kind: JournalKind;
	/** Event time; for a collapsed refusal episode, its last refusal. */
	at: string;
	strategy_id: string;
	trade_id?: string;
	asset?: string | null;
	direction?: string | null;
	size?: number | null;
	leverage?: number | null;
	book?: string | null;
	price?: number | null;
	signal_price?: number | null;
	/** Signed; positive = filled worse than the signal price. */
	slippage_bps?: number | null;
	stop_price?: number | null;
	take_profit_price?: number | null;
	risk_usd?: number | null;
	source?: string | null;
	net_pnl_usd?: number | null;
	close_reason?: string | null;
	exit_recovered_from?: string | null;
	failure_reason?: string | null;
	// Refusal episodes
	first_at?: string;
	count?: number;
	reason?: string;
	last_price?: number | null;
	/** Refused exits only: whether a position was open when the exit was refused. */
	positioned?: boolean | null;
	// Regime flags
	regime?: string | null;
	confidence?: number | null;
	gate_mode?: string | null;
	decision?: string | null;
	/** Signed return (percent) the flagged entry made over the gate's horizon. */
	mtm_pct?: number | null;
}

export interface Journal {
	mode: DeskMode;
	strategy_id: string | null;
	generated_at: string;
	window_days: number;
	episode_gap_hours: number;
	counts: Partial<Record<JournalKind, number>>;
	truncated: boolean;
	events: JournalEvent[];
}

export async function getJournal(
	mode: DeskMode,
	options: { strategyId?: string; days?: number; limit?: number } = {}
): Promise<Journal> {
	const params = new URLSearchParams({ mode });
	if (options.strategyId) params.set('strategy_id', options.strategyId);
	if (options.days) params.set('days', String(options.days));
	if (options.limit) params.set('limit', String(options.limit));
	return fetchApi(`/trading/journal?${params.toString()}`);
}

// ---- Compact fills ----

export interface DeskFill {
	id: string;
	strategy_id: string;
	asset: string | null;
	direction: string | null;
	status: 'OPEN' | 'CLOSED' | 'FAILED' | string;
	size: number | null;
	leverage: number | null;
	entry_price: number | null;
	exit_price: number | null;
	signal_entry_price: number | null;
	signal_exit_price: number | null;
	entry_slippage_bps: number | null;
	exit_slippage_bps: number | null;
	opened_at: string | null;
	closed_at: string | null;
	gross_pnl_usd: number | null;
	/** Net of recorded fees and funding (closed trades only). */
	net_pnl_usd: number | null;
	costs_usd: number | null;
	close_reason: string | null;
	exit_recovered_from: string | null;
	stop_price: number | null;
	take_profit_price: number | null;
	risk_usd: number | null;
	/** Capital the trade was sized from (live: the strategy's slice of the account). */
	slice_usd: number | null;
	book: string | null;
	regime: string | null;
	source: string | null;
	failure_reason: string | null;
}

export async function getDeskFills(mode: DeskMode, limit = 1000): Promise<{ mode: DeskMode; fills: DeskFill[]; truncated: boolean }> {
	return fetchApi(`/trading/fills?mode=${mode}&limit=${limit}`);
}

// ---- Funding and open interest ----

export interface FundingContext {
	/** Hyperliquid's hourly rate; positive = longs pay shorts. */
	rate_hourly: number;
	annualized_pct: number;
	avg_24h_hourly: number | null;
	avg_7d_hourly: number | null;
	as_of: string | null;
	stale: boolean;
	source: string | null;
	next_funding_at: string;
	interval_hours: number;
	/** [epoch ms, hourly rate] per UTC hour, oldest first. */
	series: Array<[number, number]>;
}

export interface OpenInterestContext {
	coins: number;
	usd: number | null;
	change_24h_pct: number | null;
	as_of: string | null;
	stale: boolean;
	/** [epoch ms, coins, usd] per UTC hour, oldest first. */
	series: Array<[number, number, number | null]>;
}

export interface AssetMarketContext {
	asset: string;
	funding: FundingContext | null;
	open_interest: OpenInterestContext | null;
	mark_price: number | null;
	premium: number | null;
}

export interface MarketContext {
	generated_at: string;
	hours: number;
	funding_interval_hours: number;
	assets: Record<string, AssetMarketContext>;
}

export async function getMarketContext(assets: string[], hours = 72): Promise<MarketContext> {
	const list = [...new Set(assets.map((asset) => asset.trim().toUpperCase()).filter(Boolean))];
	return fetchApi(`/trading/market-context?assets=${encodeURIComponent(list.join(','))}&hours=${hours}`);
}
