import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { writable } from 'svelte/store';
import { mount, tick, unmount } from 'svelte';

import DashboardPage from '../routes/+page.svelte';
import { load } from '../routes/+page';
import type { ForvenDashboardResponse, ForvenRiskStatus, ForvenTrade } from '$lib/api';
import type { LiveFleet, LiveFleetStrategy } from '$lib/api/dashboard';
import { forvenDashboard, forvenOpenTrades, forvenRisk } from '$lib/stores/forven';

const apiMocks = vi.hoisted(() => ({
	getForvenDashboard: vi.fn(),
	getForvenRisk: vi.fn(),
	getForvenOpenTrades: vi.fn(),
	getForvenEquityHistory: vi.fn(),
}));

const dashboardApiMocks = vi.hoisted(() => ({
	getLiveFleet: vi.fn(),
	getSchedulerJobs: vi.fn(),
	getPaperSummary: vi.fn(),
	getDashboardFunnel: vi.fn(),
	getTaskHealth: vi.fn(),
}));

const realtimeController = vi.hoisted(() => ({ start: vi.fn(), stop: vi.fn(), trigger: vi.fn() }));

vi.mock('$lib/api', () => apiMocks);
vi.mock('$lib/api/dashboard', () => dashboardApiMocks);
vi.mock('$lib/stores/forvenWebSocket', () => ({ forvenLivePrices: writable({ SOL: 120, BTC: 84000 }) }));
vi.mock('$lib/utils/realtime', () => ({ createRealtimeRefresh: vi.fn(() => realtimeController) }));
vi.mock('$lib/components/dashboard/CriticalAlertsBanner.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));
vi.mock('$lib/components/dashboard/LivePnlPanel.svelte', async () => ({
	default: (await import('./fixtures/Stub.svelte')).default,
}));

function liveStrategy(overrides: Partial<LiveFleetStrategy>): LiveFleetStrategy {
	return {
		strategy_id: 'S1',
		name: 'SOL-IV_BAND_BREAK-S1',
		display_name: null,
		symbol: 'SOL/USDT',
		timeframe: '1h',
		live_since: '2026-07-21T14:40:57+00:00',
		state: 'watching',
		open_trade_ids: [],
		trades: { closed: 2, wins: 1, losses: 1, failed: 0, win_rate: 0.5, net_pnl_usd: 23.27, last_trade_at: '2026-09-25T11:02:07+00:00' },
		last_scan: { at: new Date().toISOString(), signal_type: 'evaluate', matched: false, executed: false, reason: 'no_signal' },
		blocked_entries: { window_days: 30, count: 0, last_at: null, last_reason: null, top_reason: null, top_count: 0 },
		...overrides,
	};
}

function buildFleet(strategies: LiveFleetStrategy[]): LiveFleet {
	return {
		generated_at: new Date().toISOString(),
		stale_after_seconds: 900,
		strategies,
		live_bots_armed: 0,
		realized: {
			'7d': { closed: 4, wins: 3, losses: 1, net_pnl_usd: 24.58, win_rate: 0.75, profit_factor: 39.2 },
			'30d': { closed: 12, wins: 5, losses: 7, net_pnl_usd: 24.39, win_rate: 0.42, profit_factor: 4.99 },
			all: { closed: 66, wins: 13, losses: 52, net_pnl_usd: 15.39, win_rate: 0.2, profit_factor: 1.56 },
		},
		recent_fills: [],
	};
}

const LIVE_FLEET = buildFleet([
	liveStrategy({ strategy_id: 'S1', state: 'in_position', open_trade_ids: ['E1'] }),
	liveStrategy({
		strategy_id: 'S2',
		symbol: 'BTC/USDT',
		state: 'blocked',
		blocked_entries: {
			window_days: 30,
			count: 84,
			last_at: new Date().toISOString(),
			last_reason: 'BLOCKED BTC live — validated leverage 1.3 cannot be applied exactly at the exchange',
			top_reason: null,
			top_count: 84,
		},
	}),
]);

const DASHBOARD: ForvenDashboardResponse = {
	execution_mode: 'paper',
	live_strategy_count: 2,
	live_bot_count: 0,
	trading_allowed: true,
	daemon_running: true,
	last_scan: new Date().toISOString(),
	account: { accountValue: 1018.73, withdrawable: 1018.73, totalMarginUsed: 134.18, network: 'mainnet', source: 'books_only' },
	daily_risk: { start_equity: 1018.97, current_equity: 1018.73 },
	risk: { high_water_mark: 1032.39, drawdown_pct: 0.0132 },
	circuit_breakers: { hl_price: 'closed', hl_trade: 'closed', hl_account: 'closed' },
	prices: { SOL: 119.5 },
};

const RISK: ForvenRiskStatus = {
	kill_switch_enabled: false,
	kill_switch_active: false,
	daily_loss_halt: false,
	high_water_mark: 1032.39,
	limits: { max_drawdown: 0.3, daily_loss_limit: 0.05 },
	portfolio_budget_live: {
		total_open_risk_usd: 5.69,
		total_open_risk_limit_usd: 50.9,
		total_open_risk_used_frac: 0.11,
		stops_missing: 0,
		positions: [{ trade_id: 'E1', asset: 'SOL', direction: 'long', stop_price: 116.94, risk_usd: 5.69, notional_usd: 268.81, book: 'long' }],
		per_book: { long: { gross_notional_usd: 268.81, equity_usd: 528.63, limit_usd: 528.63, positions: 1 } },
		strategy_sizing: [{ strategy_id: 'S1', mode: 'system', effective_usd: 169.68, binding: 'slice' }],
	},
};

const OPEN_TRADES: ForvenTrade[] = [
	{ id: 'E1', asset: 'SOL', direction: 'long', strategy_id: 'S1', execution_type: 'live', entry_price: 119.47, fill_entry_price: 119.47, size: 2.25, leverage: 2, opened_at: '2026-09-25T11:02:07+00:00' },
	{ id: 'P1', asset: 'BTC', direction: 'long', strategy_id: 'S9', execution_type: 'paper', entry_price: 80688.7, size: 0.1, opened_at: '2026-09-18T11:00:00+00:00' },
];

async function flush(): Promise<void> {
	for (let i = 0; i < 4; i += 1) {
		await Promise.resolve();
		await tick();
	}
}

function text(target: HTMLElement, testId: string): string {
	return target.querySelector(`[data-testid="${testId}"]`)?.textContent?.replace(/\s+/g, ' ') ?? '';
}

describe('live-first dashboard', () => {
	let app: ReturnType<typeof mount> | null = null;
	let target: HTMLDivElement;

	function mockBackend(dashboard: ForvenDashboardResponse, fleet: LiveFleet | Error, trades = OPEN_TRADES) {
		apiMocks.getForvenDashboard.mockResolvedValue(dashboard);
		apiMocks.getForvenRisk.mockResolvedValue(RISK);
		apiMocks.getForvenOpenTrades.mockResolvedValue(trades);
		apiMocks.getForvenEquityHistory.mockResolvedValue({ base: 1000, curve: [] });
		if (fleet instanceof Error) dashboardApiMocks.getLiveFleet.mockRejectedValue(fleet);
		else dashboardApiMocks.getLiveFleet.mockResolvedValue(fleet);
		dashboardApiMocks.getSchedulerJobs.mockResolvedValue([]);
		dashboardApiMocks.getPaperSummary.mockResolvedValue({
			sessions: [],
			totals: { session_count: 16, closed_count: 69, open_count: 5, realized_pnl_usd: -6276.74, win_rate_pct: 20.3, close_reasons: {} },
			include_deployed: false,
			timestamp: '',
		});
		dashboardApiMocks.getDashboardFunnel.mockResolvedValue([
			{ state: 'generated', count: 3 },
			{ state: 'paper', count: 16 },
			{ state: 'deployed', count: 6 },
		]);
		dashboardApiMocks.getTaskHealth.mockResolvedValue({ status: 'ok', issues: [] });
	}

	async function render(fleet: LiveFleet | null) {
		app = mount(DashboardPage, { target, props: { data: { fleet } } });
		await flush();
	}

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
		forvenDashboard.set(null);
		forvenRisk.set(null);
		forvenOpenTrades.set([]);
	});

	afterEach(() => {
		if (app) unmount(app);
		app = null;
		target.remove();
		vi.clearAllMocks();
	});

	it('shows real-money positions only, the live strategy scorecard, and why entries were blocked', async () => {
		mockBackend(DASHBOARD, LIVE_FLEET);
		await render(LIVE_FLEET);

		expect(text(target, 'live-mode')).toContain('Live · mainnet · 2 strategies');
		expect(text(target, 'live-status-bar')).toContain('Kill switch disarmed');

		const positions = text(target, 'live-positions');
		expect(positions).toContain('Live positions (1)');
		expect(positions).toContain('S1');
		expect(positions).toContain('long');
		expect(positions).not.toContain('S9');

		const scorecard = text(target, 'live-strategies');
		expect(scorecard).toContain('In position');
		expect(scorecard).toContain('Blocked');
		expect(scorecard).toContain('validated leverage 1.3 cannot be applied');
		expect(scorecard).toContain('$169.68');

		expect(text(target, 'attention-panel')).toContain('S2 entries blocked (84× in 30d)');
		expect(text(target, 'realized-tile')).toContain('+$24.39');
		expect(text(target, 'ops-footer')).toContain('16 strategies · 5 open');
		expect(text(target, 'ops-footer')).toContain('live 6');
		expect(realtimeController.start).toHaveBeenCalledTimes(1);
	});

	it('falls back to a paper-only state when nothing trades real money', async () => {
		const paperOnly = buildFleet([]);
		mockBackend({ ...DASHBOARD, live_strategy_count: 0 }, paperOnly, [OPEN_TRADES[1]]);
		await render(paperOnly);

		expect(text(target, 'live-mode')).toContain('Paper only');
		expect(target.querySelector('[data-testid="no-live"]')).toBeTruthy();
		expect(target.querySelector('[data-testid="live-positions"]')).toBeNull();
	});

	it('never claims paper-only when the live counts are unknown', async () => {
		const olderBackend: ForvenDashboardResponse = { ...DASHBOARD };
		delete olderBackend.live_strategy_count;
		mockBackend(olderBackend, new Error('404'), []);
		await render(null);

		expect(text(target, 'live-mode')).toContain('Live status unknown');
		expect(target.querySelector('[data-testid="no-live"]')).toBeNull();
		expect(target.textContent).toContain('Live strategy data is unavailable right now');
	});
});

describe('dashboard loader', () => {
	it('redirects legacy ?view= tabs to /', async () => {
		for (const view of ['quant_factory', 'beta']) {
			const url = new URL(`http://localhost/?view=${view}`);
			await expect(load({ url } as never)).rejects.toMatchObject({ status: 301, location: '/' });
		}
	});

	it('returns the live fleet, or null when it cannot be loaded', async () => {
		dashboardApiMocks.getLiveFleet.mockResolvedValueOnce(LIVE_FLEET);
		expect(await load({ url: new URL('http://localhost/') } as never)).toEqual({ fleet: LIVE_FLEET });

		dashboardApiMocks.getLiveFleet.mockRejectedValueOnce(new Error('down'));
		expect(await load({ url: new URL('http://localhost/') } as never)).toEqual({ fleet: null });
	});
});
