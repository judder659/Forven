// PORT-LAYER: the portfolio layer's API surface — the measured-risk allocator
// snapshot and the funding-carry basket forward paper book.

import { fetchApi } from './core';

export interface PortfolioAllocationStrategy {
	asset: string;
	stage: string;
	measured: boolean;
	observed_days: number;
	annualized_vol: number | null;
	direction_lean: 'long' | 'short';
	risk_multiplier: number;
	weight: number | null;
}

export interface PortfolioVirtualBookStats {
	total_return: number;
	max_drawdown: number;
	sharpe: number | null;
	active_days: number;
}

export interface PortfolioAllocationSnapshot {
	computed_at: string;
	lookback_days: number;
	cohort_size: number;
	strategies: Record<string, PortfolioAllocationStrategy>;
	book: {
		measured_strategies?: number;
		unmeasured_strategies?: number;
		estimated_annualized_vol?: number | null;
		vol_target_pct?: number | null;
		vol_scale_applied?: number;
		scaled_annualized_vol?: number | null;
		virtual?: {
			weighted?: PortfolioVirtualBookStats;
			flat_baseline?: PortfolioVirtualBookStats;
			note?: string;
		};
		forward?: {
			total_return?: number;
			max_drawdown?: number;
			sharpe?: number | null;
			active_days?: number;
			since?: string;
			curve?: Array<{ t: string; equity: number }>;
			note?: string;
		};
	};
}

export interface PortfolioAllocationResponse {
	ok: boolean;
	enabled: boolean;
	live_sizing_enabled: boolean;
	snapshot: PortfolioAllocationSnapshot | null;
}

export interface BasketLeg {
	symbol: string;
	weight: number;
	funding_rate_hourly: number | null;
	carry_annualized: number | null;
}

export interface BasketTick {
	t: string;
	equity: number;
	price_pnl: number;
	funding_pnl: number;
	cost: number;
	rebalanced: boolean;
	positions: number;
}

export interface BasketSummary {
	ok: boolean;
	exists: boolean;
	enabled: boolean;
	name?: string;
	created_at?: string;
	last_tick_at?: string | null;
	tick_age_hours?: number | null;
	last_rebalance_at?: string | null;
	next_rebalance_at?: string | null;
	rebalances?: number;
	equity?: number;
	total_return_pct?: number;
	expected_carry_annualized?: number | null;
	pnl_decomposition?: { price: number; funding: number; cost: number };
	positions?: { count: number; weights: Record<string, number> };
	legs?: BasketLeg[];
	universe?: { total: number; eligible: number } | null;
	config?: {
		rebalance_hours: number;
		n_legs: number;
		gross_leverage: number;
		fee_bps: number;
		slippage_bps: number;
	};
	recent_ticks?: BasketTick[];
	equity_curve?: Array<{ t: string; equity: number }>;
}

export async function getPortfolioLayerEnabled(): Promise<boolean> {
	// PORT-GATE-1: the only portfolio route that exists while the layer is off.
	try {
		const res = await fetchApi<{ enabled: boolean }>('/api/portfolio/enabled');
		return Boolean(res?.enabled);
	} catch {
		return false;
	}
}

export async function getPortfolioAllocation(): Promise<PortfolioAllocationResponse> {
	return fetchApi('/api/portfolio/allocation');
}

export async function refreshPortfolioAllocation(): Promise<{
	ok: boolean;
	snapshot: PortfolioAllocationSnapshot | null;
}> {
	return fetchApi('/api/portfolio/allocation/refresh', { method: 'POST' });
}

export async function getPortfolioBasket(): Promise<BasketSummary> {
	return fetchApi('/api/portfolio/basket');
}

export async function tickPortfolioBasket(): Promise<{ ok: boolean; report: unknown }> {
	return fetchApi('/api/portfolio/basket/tick', { method: 'POST' });
}

export async function resetPortfolioBasket(): Promise<{ ok: boolean }> {
	return fetchApi('/api/portfolio/basket/reset', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ confirm: true }),
	});
}

export interface BasketLiveStatus {
	ok: boolean;
	armed: boolean;
	capital_usd?: number | null;
	wallet_label?: string | null;
	armed_at?: string | null;
	disarmed_at?: string | null;
	last_reconcile?: {
		t: string;
		orders_ok: number;
		orders_failed: number;
		unlistable: number;
	} | null;
	ledger?: Array<Record<string, unknown>>;
}

export async function getBasketLive(): Promise<BasketLiveStatus> {
	return fetchApi('/api/portfolio/basket/live');
}

export async function armBasketLive(
	confirm: string,
	capitalUsd: number,
	wallet: string
): Promise<{ ok: boolean; arming: Record<string, unknown> }> {
	return fetchApi('/api/portfolio/basket/golive', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ confirm, capital_usd: capitalUsd, wallet }),
	});
}

export async function disarmBasketLive(flatten: boolean): Promise<{ ok: boolean }> {
	return fetchApi('/api/portfolio/basket/disarm', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ flatten }),
	});
}
