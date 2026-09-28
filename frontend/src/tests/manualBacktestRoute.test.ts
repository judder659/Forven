import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';
import { fireEvent } from '@testing-library/svelte';
import { normalizeStrategyPayload } from '$lib/api/strategies';

const api = vi.hoisted(() => {
	class ApiError extends Error {
		status: number;
		constructor(status: number, message: string) {
			super(message);
			this.status = status;
		}
	}
	return {
		ApiError,
		getJob: vi.fn(),
		getManualBacktestDefaults: vi.fn(),
		getPrebuiltStrategies: vi.fn(),
		getResult: vi.fn(),
		getStrategies: vi.fn(),
		getSymbols: vi.fn(),
		previewSignals: vi.fn(),
		submitBacktest: vi.fn(),
	};
});
const appState = vi.hoisted(() => ({ url: new URL('http://localhost/backtest/new') }));
vi.mock('$lib/api', () => api);
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));
vi.mock('$app/stores', () => ({
	page: {
		subscribe(callback: (value: { url: URL }) => void) {
			callback({ url: appState.url });
			return () => {};
		},
	},
}));
vi.mock('$lib/stores/processTracker', () => ({ addToast: vi.fn() }));
vi.mock('$lib/components/EquityChart.svelte', async () => ({ default: (await import('./stubs/EquityChartStub.svelte')).default }));

import ManualBacktestPage from '../routes/backtest/new/+page.svelte';

const catalog = normalizeStrategyPayload({
	strategies: [
		{
			name: 'ETH 15m EMA Trend', api_name: 'ema_cross_eth_15m', type: 'ema_cross_eth_15m', version: '1.0.0',
			description: 'EMA trend', source: 'prebuilt', asset: 'ETH', timeframe: '15m',
			trade_modes: ['long_only'], default_trade_mode: 'long_only',
			parameters: {
				fast: { type: 'number', default: 12 },
				timeframe: { type: 'str', default: '15m' },
				leverage: { type: 'number', default: 1.0 },
			},
		},
		{
			name: 'Stochastic LONG', api_name: 'stochastic', type: 'stochastic', version: '1.0.0',
			description: 'Stochastic', source: 'prebuilt', asset: 'BTC', timeframe: null,
			trade_modes: ['long_only', 'short_only'], default_trade_mode: 'long_only',
			parameters: { k: { type: 'number', default: 14 }, leverage: { type: 'number', default: 3.0 } },
		},
	],
});

const paperRow = {
	id: 'S07239', display_id: 'S07239', name: 'BTC-BTC_KC63202_PULLBACK_THRUST-S07239',
	symbol: 'BTC/USDT', timeframe: '4h', stage: 'paper', status: 'paper', source: 'ai_dropzone',
	params: JSON.stringify({
		kc_period: 20, leverage: 1.3, _asset: 'BTC',
		execution_profile: { sizing_mode: 'atr', risk_per_trade: 0.01, atr_stop_multiplier: 2.5 },
	}),
};

const result = {
	id: 'R1', result_id: 'R1', job_id: 'bt_1', strategy_name: 'S07239', strategy_id: 'S07239', strategy_version: '1',
	symbol: 'BTC', timeframe: '4h', created_at: '', start: '2024-12-23T06:00:00+00:00', end: '2025-12-31T23:00:00+00:00',
	config: { warnings: ['Leverage is capped live.'] },
	metrics: {
		total_return: -0.15808, sharpe_ratio: 0.4, max_drawdown: 0.18133, win_rate: 0.36, total_trades: 50,
		cagr: -0.42921, annualized_return_pct: -0.42921, profit_factor: 0.53,
		in_sample: { start_date: '2024-12-23T06:00:00+00:00', end_date: '2025-09-10T21:00:00+00:00', sharpe: 2.1, total_return_pct: 0.25, total_trades: 90, win_rate: 0.5, max_drawdown_pct: 0.125 },
		out_of_sample: { start_date: '2025-09-10T21:00:00+00:00', end_date: '2025-12-31T23:00:00+00:00', sharpe: 0.4, total_return_pct: -0.15808, total_trades: 50, win_rate: 0.36, max_drawdown_pct: 0.18133 },
	},
	equity_curve: [],
	trades: [],
};

let target: HTMLDivElement;
let app: ReturnType<typeof mount> | null = null;

async function settle() {
	for (let i = 0; i < 12; i += 1) {
		await Promise.resolve();
		await tick();
	}
}
function text() {
	return target.textContent ?? '';
}
function button(label: string | RegExp) {
	return [...target.querySelectorAll('button')].find((b) => {
		const t = b.textContent?.replace(/\s+/g, ' ').trim() ?? '';
		return typeof label === 'string' ? t === label : label.test(t);
	}) as HTMLButtonElement;
}
async function choose(key: string) {
	const select = target.querySelector('#bt-strategy') as HTMLSelectElement;
	select.value = key;
	await fireEvent.change(select);
	await settle();
}
async function start(url = 'http://localhost/backtest/new') {
	appState.url = new URL(url);
	app = mount(ManualBacktestPage, { target });
	await settle();
}

beforeEach(() => {
	vi.clearAllMocks();
	sessionStorage.clear();
	api.getPrebuiltStrategies.mockResolvedValue(catalog);
	api.getSymbols.mockResolvedValue(['BTC/USDT', 'ETH/USDT']);
	api.getManualBacktestDefaults.mockResolvedValue({
		fee_bps: 4.5, slippage_bps: 2, initial_capital: 10000, leverage: 1, duration_days: 730,
		include_funding: true, holdout_cutoff: null,
	});
	api.getStrategies.mockImplementation(async ({ status }: { status?: string } = {}) =>
		normalizeStrategyPayload(status === 'paper' ? [paperRow] : []));
	target = document.createElement('div');
	document.body.appendChild(target);
});

afterEach(async () => {
	if (app) await unmount(app);
	app = null;
	target.remove();
	vi.useRealTimers();
});

describe('Manual Backtest page', () => {
	it('lists built-ins without fetching the full strategy table', async () => {
		await start();
		expect(api.getPrebuiltStrategies).toHaveBeenCalledTimes(1);
		expect(api.getStrategies).not.toHaveBeenCalled();
		const options = [...target.querySelectorAll('#bt-strategy option')].map((o) => o.textContent?.trim());
		expect(options).toEqual(['Select a strategy…', 'ETH 15m EMA Trend', 'Stochastic LONG']);
	});

	it('loads live, paper and Forge strategies on demand and runs one on its own market and settings', async () => {
		await start();
		await fireEvent.click(button(/^My strategies/));
		await settle();
		expect(api.getStrategies.mock.calls.map(([arg]) => arg.status)).toEqual(['live_graduated', 'paper', 'gauntlet', 'quick_screen']);
		expect(target.querySelector('optgroup')?.getAttribute('label')).toBe('Paper');

		await choose('S07239');
		expect((target.querySelector('#bt-symbol') as HTMLInputElement).value).toBe('BTC/USDT');
		expect((target.querySelector('#bt-timeframe') as HTMLSelectElement).value).toBe('4h');
		// The execution summary reports the strategy's own profile, not "full equity".
		expect(text()).toContain("Strategy's own profile: 1% risk per trade, 2.5× ATR stop");

		api.submitBacktest.mockResolvedValue({ job_id: 'bt_1', result_id: 'R1', status: 'queued' });
		api.getJob.mockResolvedValue({ id: 'bt_1', status: 'queued' });
		await fireEvent.click(button('Run backtest'));
		await settle();
		const [request, options] = api.submitBacktest.mock.calls[0];
		expect(options).toEqual({ background: true });
		expect(request).toEqual({
			strategy_id: 'S07239', strategy_name: 'S07239', symbol: 'BTC/USDT', timeframe: '4h',
			start: expect.any(String), end: expect.any(String), preserve_result: true,
		});
	});

	it('runs a built-in on the market it was written for, with its template defaults', async () => {
		await start();
		await choose('ema_cross_eth_15m');
		expect((target.querySelector('#bt-symbol') as HTMLInputElement).value).toBe('ETH/USDT');
		expect((target.querySelector('#bt-timeframe') as HTMLSelectElement).value).toBe('15m');

		api.submitBacktest.mockResolvedValue({ job_id: 'bt_1', result_id: 'R1', status: 'queued' });
		api.getJob.mockResolvedValue({ id: 'bt_1', status: 'queued' });
		await fireEvent.click(button('Run backtest'));
		await settle();
		expect(api.submitBacktest.mock.calls[0][0].params).toEqual({ fast: 12, timeframe: '15m', leverage: 1 });
	});

	it('follows the job and shows what was actually tested', async () => {
		vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval'] });
		await start();
		await choose('stochastic');
		api.submitBacktest.mockResolvedValue({ job_id: 'bt_1', result_id: 'R1', status: 'queued' });
		api.getJob
			.mockResolvedValueOnce({ id: 'bt_1', status: 'running', progress: 'Running backtest and saving results' })
			.mockResolvedValue({ id: 'bt_1', status: 'succeeded', result_id: 'R1' });
		api.getResult.mockResolvedValue(result);

		await fireEvent.click(button('Run backtest'));
		await settle();
		expect(text()).toContain('Running backtest and saving results');
		await vi.advanceTimersByTimeAsync(1600);
		await settle();

		expect(api.getResult).toHaveBeenCalledWith('R1');
		expect(text()).toContain('Tested Dec 23, 2024 → Dec 31, 2025');
		expect(text()).toContain('out-of-sample only: Sep 10, 2025 → Dec 31, 2025');
		expect(text()).toContain('In-sample vs out-of-sample');
		expect(text()).toContain('less than half the in-sample figure');
		expect(text()).toContain('Leverage is capped live.');
		// CAGR is a fraction: -0.42921 is -42.92%, not -0.43%.
		expect(text()).toContain('-42.92%');
		expect(text()).toContain('Runs this session');
		// The run survives a reload of the page.
		expect(JSON.parse(sessionStorage.getItem('forven.manualBacktest.runs.v1') ?? '[]')[0]).toMatchObject({
			resultId: 'R1', status: 'succeeded', summary: { trades: 50 },
		});
	});

	it('ends the window at the research holdout cutoff', async () => {
		api.getManualBacktestDefaults.mockResolvedValue({
			fee_bps: 4.5, slippage_bps: 2, initial_capital: 10000, leverage: 1, duration_days: 730,
			include_funding: true, holdout_cutoff: '2026-01-01T00:00:00+00:00',
		});
		await start();
		expect((target.querySelector('#bt-date-end') as HTMLInputElement).value).toBe('2026-01-01');
		expect((target.querySelector('#bt-date-start') as HTMLInputElement).value).toBe('2024-01-01');
		expect(text()).toContain('Research holdout');
	});

	it('offers only the trade modes a template supports, and never Kelly', async () => {
		await start();
		await choose('ema_cross_eth_15m');
		await fireEvent.click(button(/^Execution/));
		await settle();
		const direction = [...target.querySelectorAll('select')].find((s) => s.textContent?.includes('Strategy default'))!;
		expect([...direction.options].map((o) => o.textContent?.trim())).toEqual(['Strategy default (long only)', 'Long only']);
		// The leverage placeholder is the template's own, not a hard 1×.
		const leverage = [...target.querySelectorAll('input[type="number"]')].find((i) => (i as HTMLInputElement).placeholder.includes('×')) as HTMLInputElement;
		expect(leverage.placeholder).toBe('1× (strategy)');

		await fireEvent.change(target.querySelector('input[type="checkbox"]') as HTMLInputElement);
		await settle();
		const kelly = [...target.querySelectorAll('option')].find((o) => o.value === 'kelly') as HTMLOptionElement;
		expect(kelly.disabled).toBe(true);
	});

	it('preselects a strategy from the link', async () => {
		await start('http://localhost/backtest/new?strategy=S07239');
		expect(api.getStrategies).toHaveBeenCalled();
		expect(text()).toContain('S07239 · BTC 4h');
		expect((target.querySelector('#bt-timeframe') as HTMLSelectElement).value).toBe('4h');
	});

	it('explains a full backtest queue', async () => {
		await start();
		await choose('stochastic');
		api.submitBacktest.mockRejectedValue(new api.ApiError(429, 'Backtest queue is full; wait for an existing run to finish.'));
		await fireEvent.click(button('Run backtest'));
		await settle();
		expect(text()).toContain('The backtest queue is full');
	});
});
