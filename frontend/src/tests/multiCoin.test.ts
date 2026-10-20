import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/svelte';
import CoinBreakdown from '../lib/components/strategy/container/CoinBreakdown.svelte';
import { parseCoinBreakdown, parseMultiCoin } from '$lib/api/multiCoin';
import { forwardIndex } from '$lib/utils/forge/forward';
import { buildRows } from '$lib/utils/tradingDesk/rows';
import type { PaperSummary } from '$lib/api/dashboard';
import type { PaperTradingSession } from '$lib/api/paper';

const CONTAINER = {
	coins: ['ETH', 'UNI', 'NEAR'],
	symbols: ['ETH/USDT', 'UNI/USDT', 'NEAR/USDT'],
	home: 'ETH',
	frozen_at: '2026-10-19T10:00:00+00:00',
	source: 'breadth',
	breadth_verdict: 'general',
	weight: 0.333333,
	trading: true,
};

describe('multi-coin containers', () => {
	it('parses the frozen list and treats fewer than two coins as single-coin', () => {
		expect(parseMultiCoin(null)).toBeNull();
		expect(parseMultiCoin({ coins: ['ETH'] })).toBeNull();
		const info = parseMultiCoin({ ...CONTAINER, by_coin: [{ coin: 'uni', closed_count: 2, win_rate_pct: 50, realized_pnl_usd: 3 }] });
		expect(info).toMatchObject({ coins: ['ETH', 'UNI', 'NEAR'], home: 'ETH', trading: true });
		expect(info?.by_coin).toEqual([
			expect.objectContaining({ coin: 'UNI', closed_count: 2, win_rate_pct: 50, realized_pnl_usd: 3, in_container: true }),
		]);
		expect(parseCoinBreakdown([{ coin: '' }, { coin: 'ETH', win_rate_pct: null }])).toEqual([
			expect.objectContaining({ coin: 'ETH', win_rate_pct: null, open_sides: [] }),
		]);
	});

	it('carries the coin list and per-coin record onto the forward record', () => {
		const breakdown = parseCoinBreakdown([{ coin: 'ETH', closed_count: 1 }, { coin: 'UNI', open_count: 1 }]);
		const paper = {
			sessions: [
				{ session_id: 'x', strategy_id: 'S1', strategy_name: 'S1', symbol: 'ETH/USDT', timeframe: '1h', status: 'watching', closed_count: 1, open_count: 1, realized_pnl_usd: 2, win_rate_pct: 100, close_reasons: {}, multi_coin: parseMultiCoin(CONTAINER), coin_breakdown: breakdown },
				{ session_id: 'y', strategy_id: 'S2', strategy_name: 'S2', symbol: 'BTC/USDT', timeframe: '1h', status: 'watching', closed_count: 0, open_count: 0, realized_pnl_usd: 0, win_rate_pct: null, close_reasons: {}, multi_coin: null, coin_breakdown: null },
			],
			totals: { session_count: 2, closed_count: 1, open_count: 1, realized_pnl_usd: 2, win_rate_pct: 100, close_reasons: {} },
			include_deployed: false,
			timestamp: '',
		} as PaperSummary;
		const index = forwardIndex(paper, null);
		expect(index.get('S1')?.multiCoin?.coins).toEqual(['ETH', 'UNI', 'NEAR']);
		expect(index.get('S1')?.byCoin?.map((row) => row.coin)).toEqual(['ETH', 'UNI']);
		expect(index.get('S2')?.multiCoin).toBeNull();
	});

	it('marks another coin of a container on its own price on the desk', () => {
		const session = {
			id: 'compat:strategy:S1:1',
			strategy_id: 'S1',
			strategy_name: 'S1',
			symbol: 'ETH/USDT',
			timeframe: '1h',
			leverage: 1,
			status: 'position_open',
			current_price: 2500,
			positions: [
				{ id: 'T1', symbol: 'UNI/USDT', side: 'long', entry_price: 5, entry_time: '2026-10-20T08:00:00Z', size: 10, current_price: 6, unrealized_pnl: 10, unrealized_pnl_pct: 20 },
			],
		} as unknown as PaperTradingSession;
		const [row] = buildRows({ mode: 'paper', sessions: [session], fleet: null, stats: new Map(), prices: { ETH: 2600, UNI: 6.5 }, now: Date.parse('2026-10-20T12:00:00Z') });
		expect(row.legs[0].symbol).toBe('UNI/USDT');
		expect(row.legMath[0].mark).toBe(6.5);
		expect(row.legMath[0].pnl).toBeCloseTo(15); // 10 + (6.5 - 6) * 10, never ETH's 2600
	});

	describe('coin table', () => {
		afterEach(() => cleanup());

		it('lists every coin with its record, home first, and says when the switch is off', () => {
			const info = parseMultiCoin({
				...CONTAINER,
				trading: false,
				by_coin: [
					{ coin: 'ETH', home: true, closed_count: 4, wins: 1, win_rate_pct: 25, realized_pnl_usd: -120 },
					{ coin: 'UNI', closed_count: 2, wins: 2, win_rate_pct: 100, realized_pnl_usd: 40, open_count: 1, open_sides: ['long'] },
					{ coin: 'NEAR' },
				],
			});
			const { container } = render(CoinBreakdown, { props: { info: info! } });
			const coins = [...container.querySelectorAll('tr[data-coin]')].map((row) => row.getAttribute('data-coin'));
			expect(coins).toEqual(['ETH', 'UNI', 'NEAR']);
			expect(container.querySelector('tr[data-coin="UNI"]')?.textContent).toContain('long');
			expect(container.querySelector('tr[data-coin="UNI"]')?.textContent).toContain('+$40');
			expect(screen.getByText(/2 of 3 have traded/)).toBeTruthy();
			expect(screen.getByText(/switched off in Settings, so only ETH trades/)).toBeTruthy();
		});
	});
});
