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
	};
}

export interface PortfolioAllocationResponse {
	ok: boolean;
	enabled: boolean;
	live_sizing_enabled: boolean;
	snapshot: PortfolioAllocationSnapshot | null;
}

export interface BasketSummary {
	ok: boolean;
	exists: boolean;
	enabled: boolean;
	name?: string;
	created_at?: string;
	last_tick_at?: string | null;
	last_rebalance_at?: string | null;
	rebalances?: number;
	equity?: number;
	total_return_pct?: number;
	pnl_decomposition?: { price: number; funding: number; cost: number };
	positions?: { count: number; weights: Record<string, number> };
	equity_curve?: Array<{ t: string; equity: number }>;
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
