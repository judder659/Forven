import { fetchApi } from './core';

// Breadth test (forven.breadth): a strategy's frozen rule run on 15 coins over
// sealed research data, to see whether its edge travels beyond its home coin.

export interface BreadthCoin {
	asset: string;
	error?: string;
	trades?: number;
	return_pct?: number;
	sharpe?: number | null;
	max_drawdown_pct?: number;
	buy_hold_return_pct?: number | null;
	start?: string | null;
	end?: string | null;
}

export type BreadthVerdict = 'general' | 'home_only' | 'mixed' | 'too_few';

export interface BreadthSummary {
	verdict: BreadthVerdict;
	home: string;
	home_positive: boolean;
	home_sharpe_rank: number | null;
	ranked_coins: number;
	other_coins: number;
	other_traded: number;
	other_positive: number;
	share_positive: number | null;
	median_sharpe: number | null;
	sign_test_p: number | null;
	untestable: string[];
	no_trades: string[];
}

export interface BreadthState {
	strategy_id: string;
	status: 'none' | 'running' | 'done' | 'error' | 'interrupted';
	home?: string;
	timeframe?: string;
	assets?: string[];
	rows?: BreadthCoin[];
	summary?: BreadthSummary;
	stale?: boolean;
	error?: string;
	started_at?: string;
	finished_at?: string;
	stamp?: { cutoff?: string | null; [key: string]: unknown };
	busy_with?: string[];
}

export async function getStrategyBreadth(strategyId: string): Promise<BreadthState> {
	return fetchApi(`/api/strategies/${encodeURIComponent(strategyId)}/breadth`);
}

export async function startStrategyBreadth(strategyId: string, refresh = false): Promise<BreadthState> {
	return fetchApi(`/api/strategies/${encodeURIComponent(strategyId)}/breadth?refresh=${refresh}`, { method: 'POST' });
}
