// Multi-coin paper containers (backend forven.multi_coin): one rule paper-trading
// several coins under one strategy id. The strategy row keeps its home symbol;
// these shapes carry the frozen coin list and each coin's share of the record.
// Sent on GET /paper/summary sessions and GET /strategies/{id}/container.

import { asArray, asRecord, fetchApi } from './core';

export interface MultiCoinInfo {
	/** Home coin first, then the coins the breadth test added. */
	coins: string[];
	symbols: string[];
	home: string;
	frozen_at: string | null;
	source: string | null;
	breadth_verdict: string | null;
	/** Share of the strategy's paper equity each coin sizes on (1 / coins). */
	weight: number;
	/** False when multi-coin paper is switched off: the list is kept, only the home coin trades. */
	trading: boolean;
	/** The breadth test's own pick; any other coin was added by hand and carries no breadth evidence. */
	tested_coins: string[];
	hand_edited: boolean;
	edited_at: string | null;
	/** Strategy container payload only. */
	by_coin: CoinForward[];
}

export interface CoinForward {
	coin: string;
	home: boolean;
	/** False for a coin traded outside the frozen list (before the freeze). */
	in_container: boolean;
	closed_count: number;
	wins: number;
	win_rate_pct: number | null;
	realized_pnl_usd: number;
	open_count: number;
	open_sides: string[];
	/** Paper summary only; 0 on the strategy container payload. */
	unrealized_pnl_usd: number;
	last_closed_at: string | null;
}

function num(value: unknown, fallback = 0): number {
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : fallback;
}

function str(value: unknown): string | null {
	const text = String(value ?? '').trim();
	return text ? text : null;
}

export function parseCoinBreakdown(value: unknown): CoinForward[] {
	return asArray(value)
		.map((item) => asRecord(item))
		.filter((rec): rec is Record<string, unknown> => Boolean(rec && str(rec.coin)))
		.map((rec) => ({
			coin: String(rec.coin).trim().toUpperCase(),
			home: Boolean(rec.home),
			in_container: rec.in_container !== false,
			closed_count: num(rec.closed_count),
			wins: num(rec.wins),
			win_rate_pct: rec.win_rate_pct == null ? null : num(rec.win_rate_pct, Number.NaN),
			realized_pnl_usd: num(rec.realized_pnl_usd),
			open_count: num(rec.open_count),
			open_sides: asArray(rec.open_sides).map((side) => String(side)),
			unrealized_pnl_usd: num(rec.unrealized_pnl_usd),
			last_closed_at: str(rec.last_closed_at),
		}))
		.map((row) => (row.win_rate_pct !== null && !Number.isFinite(row.win_rate_pct) ? { ...row, win_rate_pct: null } : row));
}

/** The container block, or null for a single-coin strategy. */
export function parseMultiCoin(value: unknown): MultiCoinInfo | null {
	const rec = asRecord(value);
	if (!rec) return null;
	const coins = asArray(rec.coins).map((coin) => String(coin ?? '').trim().toUpperCase()).filter(Boolean);
	if (coins.length < 2) return null;
	return {
		coins,
		symbols: asArray(rec.symbols).map((symbol) => String(symbol ?? '')),
		home: str(rec.home)?.toUpperCase() ?? coins[0],
		frozen_at: str(rec.frozen_at),
		source: str(rec.source),
		breadth_verdict: str(rec.breadth_verdict),
		weight: num(rec.weight, 1 / coins.length),
		trading: rec.trading !== false,
		tested_coins: coinsOf(rec.tested_coins),
		hand_edited: Boolean(rec.hand_edited),
		edited_at: str(rec.edited_at),
		by_coin: parseCoinBreakdown(rec.by_coin),
	};
}

function coinsOf(value: unknown): string[] {
	return asArray(value).map((coin) => String(coin ?? '').trim().toUpperCase()).filter(Boolean);
}

/** Any strategy's editable coin list: the home coin first, then any extra coins. */
export interface CoinList {
	home: string;
	coins: string[];
	tested_coins: string[];
	edited_at: string | null;
	max_coins: number;
}

export function parseCoinList(value: unknown): CoinList | null {
	const rec = asRecord(value);
	const home = str(rec?.home)?.toUpperCase();
	if (!rec || !home) return null;
	const coins = coinsOf(rec.coins);
	return {
		home,
		coins: coins[0] === home ? coins : [home],
		tested_coins: coinsOf(rec.tested_coins),
		edited_at: str(rec.edited_at),
		max_coins: num(rec.max_coins, 15),
	};
}

/** Set the coins a strategy paper-trades. The home coin always stays; extras record from now. */
export async function setStrategyCoins(strategyId: string, coins: string[]): Promise<CoinList | null> {
	const data = asRecord(
		await fetchApi<unknown>(`/strategies/${encodeURIComponent(strategyId)}/coins`, {
			method: 'PUT',
			body: JSON.stringify({ coins }),
		}),
	);
	return parseCoinList(data?.coin_list);
}
