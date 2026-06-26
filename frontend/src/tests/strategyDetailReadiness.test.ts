import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';
import type { PipelineSettings } from '../lib/api/lifecycle';

const apiMocks = vi.hoisted(() => ({
	deleteResult: vi.fn(),
	getDatasets: vi.fn(),
	getJob: vi.fn(),
	getPipelineSettings: vi.fn(),
	getResult: vi.fn(),
	getResultChartContext: vi.fn(),
	getStrategyContainer: vi.fn(),
	promoteForvenStrategy: vi.fn(),
	submitBacktest: vi.fn(),
	submitOptimization: vi.fn(),
}));

const backtestingMocks = vi.hoisted(() => ({
	updateStrategyDefaultParams: vi.fn(),
}));

const toastMocks = vi.hoisted(() => ({
	addToast: vi.fn(),
}));

const appMocks = vi.hoisted(() => ({
	goto: vi.fn(),
	pageValue: {
		params: { id: 'S0001' },
		url: new URL('http://localhost/lab/strategy/S0001'),
	},
}));

vi.mock('$lib/api', () => apiMocks);
vi.mock('$lib/api/backtesting', () => backtestingMocks);
vi.mock('$lib/stores/processTracker', () => ({
	addToast: toastMocks.addToast,
}));
vi.mock('$app/navigation', () => ({
	goto: appMocks.goto,
}));
vi.mock('$app/stores', () => ({
	page: {
		subscribe(callback: (value: typeof appMocks.pageValue) => void) {
			callback(appMocks.pageValue);
			return () => {};
		},
	},
}));
vi.mock('$lib/components/ui/PromotionReadiness.svelte', async () => {
	const module = await import('./stubs/PromotionReadinessStub.svelte');
	return { default: module.default };
});
vi.mock('$lib/components/robustness/RobustnessPanel.svelte', async () => {
	const module = await import('./stubs/RobustnessPanelStub.svelte');
	return { default: module.default };
});

import StrategyDetailPage from '../routes/lab/strategy/[id]/+page.svelte';
import PromotionReadinessStub from './stubs/PromotionReadinessStub.svelte';

type MountedComponent = ReturnType<typeof mount>;

const pipelineSettings: PipelineSettings = {
	version: 1,
	autopilot_enabled: false,
	autopilot_worker_concurrency: 1,
	autopilot_generation_batch_size: 1,
	autopilot_scan_symbol: 'BTC/USDT',
	autopilot_scan_timeframe: '1h',
	promotion_mode: 'quick_screen',
	min_backtest_trades: 20,
	min_sharpe_ratio: 0.5,
	max_drawdown_pct: 40,
	min_profit_factor: 1.2,
	min_paper_days: 0,
	max_paper_divergence_pct: 0,
	min_paper_trades: 0,
	min_paper_sharpe: 0,
	failed_retention_hours: 24,
	ranking_top_n: 10,
	ranking_metric: 'sharpe_ratio',
	created_at: '2026-04-01T00:00:00Z',
	created_by: 'brain',
};

function buildContainer(): Record<string, unknown> {
	return {
		strategy: {
			id: 'S0001',
			name: 'BTC-MACD-S0001',
			state: 'quick_screen',
			source: 'manual',
			source_ref: null,
			owner: 'brain',
			symbol: 'BTC/USDT',
			timeframe: '1h',
			definition_json: null,
			dataset_hash: null,
			policy_version: 1,
			build_version: null,
			metrics_json: null,
			metrics: {
				in_sample_sharpe: 0.82,
				total_return_pct: 12.4,
				max_drawdown_pct: 18.7,
			},
			paper_session_id: null,
			paper_started_at: null,
			last_policy_result_json: null,
			blocked_reason: null,
			model: null,
			model_id: null,
			created_at: '2026-03-01T00:00:00Z',
			updated_at: '2026-03-01T00:00:00Z',
			state_changed_at: null,
			failed_at: null,
			retention_expires_at: null,
		},
		configuration: {
			symbol: 'BTC/USDT',
			timeframe: '1h',
			params: {
				fast: 12,
				slow: 26,
			},
			type: 'manual',
			owner: 'brain',
			stage: 'quick_screen',
		},
		history: {
			all: [],
			backtests: [
				{
					result_id: 'B1001',
					strategy_id: 'S0001',
					result_type: 'backtest',
					symbol: 'BTC/USDT',
					timeframe: '1h',
					start_date: '2025-01-01T00:00:00Z',
					end_date: '2025-12-31T00:00:00Z',
					metrics: {
						in_sample_sharpe: 0.82,
						total_return_pct: 12.4,
						max_drawdown_pct: 18.7,
					},
					config: {},
					created_at: '2026-03-01T00:00:00Z',
					deleted_at: null,
				},
			],
			optimizations: [],
			walk_forward: [],
			validation: [
				{
					result_id: 'V1001',
					strategy_id: 'S0001',
					result_type: 'walk_forward',
					symbol: 'BTC/USDT',
					timeframe: '1h',
					start_date: '2025-01-01T00:00:00Z',
					end_date: '2025-12-31T00:00:00Z',
					metrics: {},
					config: {},
					created_at: '2026-03-01T00:00:00Z',
					deleted_at: null,
				},
				{
					result_id: 'V1002',
					strategy_id: 'S0001',
					result_type: 'monte_carlo',
					symbol: 'BTC/USDT',
					timeframe: '1h',
					start_date: '2025-01-01T00:00:00Z',
					end_date: '2025-12-31T00:00:00Z',
					metrics: {},
					config: {},
					created_at: '2026-03-01T00:00:00Z',
					deleted_at: null,
				},
				{
					result_id: 'V1003',
					strategy_id: 'S0001',
					result_type: 'param_jitter',
					symbol: 'BTC/USDT',
					timeframe: '1h',
					start_date: '2025-01-01T00:00:00Z',
					end_date: '2025-12-31T00:00:00Z',
					metrics: {},
					config: {},
					created_at: '2026-03-01T00:00:00Z',
					deleted_at: null,
				},
				{
					result_id: 'V1004',
					strategy_id: 'S0001',
					result_type: 'cost_stress',
					symbol: 'BTC/USDT',
					timeframe: '1h',
					start_date: '2025-01-01T00:00:00Z',
					end_date: '2025-12-31T00:00:00Z',
					metrics: {},
					config: {},
					created_at: '2026-03-01T00:00:00Z',
					deleted_at: null,
				},
				{
					result_id: 'V1005',
					strategy_id: 'S0001',
					result_type: 'regime_split',
					symbol: 'BTC/USDT',
					timeframe: '1h',
					start_date: '2025-01-01T00:00:00Z',
					end_date: '2025-12-31T00:00:00Z',
					metrics: {},
					config: {},
					created_at: '2026-03-01T00:00:00Z',
					deleted_at: null,
				},
			],
		},
		execution: {
			trades: [],
			positions: [],
		},
		events: [],
	};
}

async function flush(): Promise<void> {
	await Promise.resolve();
	await tick();
	await Promise.resolve();
	await tick();
}

async function waitForCondition(predicate: () => boolean, attempts = 20): Promise<void> {
	for (let index = 0; index < attempts; index += 1) {
		if (predicate()) {
			return;
		}
		await flush();
	}
	throw new Error('Timed out waiting for strategy detail route state.');
}

describe('Strategy detail promotion readiness routing', () => {
	let target: HTMLDivElement;
	let app: MountedComponent | null = null;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
		apiMocks.deleteResult.mockReset();
		apiMocks.getDatasets.mockReset();
		apiMocks.getJob.mockReset();
		apiMocks.getPipelineSettings.mockReset();
		apiMocks.getResult.mockReset();
		apiMocks.getResultChartContext.mockReset();
		apiMocks.getStrategyContainer.mockReset();
		apiMocks.promoteForvenStrategy.mockReset();
		apiMocks.submitBacktest.mockReset();
		apiMocks.submitOptimization.mockReset();
		backtestingMocks.updateStrategyDefaultParams.mockReset();
		toastMocks.addToast.mockReset();
		apiMocks.getDatasets.mockResolvedValue([]);
		apiMocks.getPipelineSettings.mockResolvedValue(pipelineSettings);
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer());
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
		vi.clearAllMocks();
	});

	it('routes run_validation_suite actions to the robustness sub-tab', async () => {
		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('[data-testid="promotion-readiness-stub"]') !== null);

		const button = target.querySelector('[data-testid="trigger-validation-suite"]');
		expect(button).not.toBeNull();
		button?.dispatchEvent(new MouseEvent('click', { bubbles: true }));

		await waitForCondition(() => target.querySelector('[data-testid="robustness-panel-stub"]') !== null);

		expect(target.textContent).toContain('Robustness Panel Stub');
	});

	it('passes three derived quick screen rows into PromotionReadiness', async () => {
		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('[data-testid="promotion-readiness-stub"]') !== null);

		expect(apiMocks.getPipelineSettings).toHaveBeenCalledTimes(1);
		// The validation-coverage row was dropped from quick-screen readiness
		// (commit c86a782), so buildQuickScreenEvidenceRows now derives 3 rows.
		expect(target.querySelector('[data-testid="stub-quick-screen-row-count"]')?.textContent).toBe('3');
		expect(target.querySelector('[data-testid="stub-quick-screen-first-label"]')?.textContent).toBe(
			'IS Sharpe Ratio'
		);
	});
});

describe('PromotionReadiness test stub', () => {
	let target: HTMLDivElement;
	let app: MountedComponent | null = null;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
	});

	it('shows a none sentinel when quickScreenRows is empty', async () => {
		app = mount(PromotionReadinessStub, { target });
		await flush();

		expect(target.querySelector('[data-testid="stub-quick-screen-first-label"]')?.textContent).toBe('none');
	});
});
