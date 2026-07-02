import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';

const apiMocks = vi.hoisted(() => ({
	deleteResult: vi.fn(),
	getDatasets: vi.fn(),
	getJob: vi.fn(),
	getPipelineSettings: vi.fn(),
	getResult: vi.fn(),
	getResultChartContext: vi.fn(),
	getPrebuiltStrategies: vi.fn(),
	getStrategyContainer: vi.fn(),
	submitBacktest: vi.fn(),
	submitOptimization: vi.fn(),
}));

const backtestingMocks = vi.hoisted(() => ({
	getRobustnessResult: vi.fn(),
	getStrategyOpenPosition: vi.fn(),
	runCostStressRobustness: vi.fn(),
	runMonteCarloRobustness: vi.fn(),
	runParamJitterRobustness: vi.fn(),
	runRegimeSplitRobustness: vi.fn(),
	runWalkForwardRobustness: vi.fn(),
	submitWalkForwardRobustness: vi.fn(),
	updateStrategyDefaultParams: vi.fn(),
}));

const lifecycleMocks = vi.hoisted(() => ({
	getGauntletStatus: vi.fn(),
	getPaperLiveReadiness: vi.fn(),
	getPipelineConfig: vi.fn(),
	getPromotionReadiness: vi.fn(),
	getStrategyExecutionGrowth: vi.fn(),
	runTimeframeSweep: vi.fn(),
}));

const appMocks = vi.hoisted(() => ({
	goto: vi.fn(),
	pageValue: {
		params: { id: 'S0001' },
		url: new URL('http://localhost/lab/strategy/S0001'),
	},
}));

const toastMocks = vi.hoisted(() => ({
	addToast: vi.fn(),
	trackProcess: vi.fn(),
	trackedProcesses: (() => {
		type Subscriber<T> = (value: T) => void;
		let value: unknown[] = [];
		const subscribers = new Set<Subscriber<unknown[]>>();
		return {
			subscribe(callback: Subscriber<unknown[]>) {
				callback(value);
				subscribers.add(callback);
				return () => subscribers.delete(callback);
			},
			set(nextValue: unknown[]) {
				value = nextValue;
				for (const subscriber of subscribers) {
					subscriber(value);
				}
			},
		};
	})(),
}));

vi.mock('$lib/api', () => apiMocks);
vi.mock('$lib/api/strategies', () => ({
	getPrebuiltStrategies: apiMocks.getPrebuiltStrategies,
}));
vi.mock('$lib/api/backtesting', () => backtestingMocks);
vi.mock('$lib/api/lifecycle', () => lifecycleMocks);
vi.mock('$lib/stores/processTracker', () => ({
	addToast: toastMocks.addToast,
	trackProcess: toastMocks.trackProcess,
	trackedProcesses: toastMocks.trackedProcesses,
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
// lightweight-charts needs a real canvas 2D context, which jsdom lacks — stub the
// chart component so Overview cards that render it (e.g. Strategy Growth) mount.
vi.mock('$lib/components/EquityChart.svelte', async () => {
	const module = await import('./stubs/EquityChartStub.svelte');
	return { default: module.default };
});

import StrategyDetailPage from '../routes/lab/strategy/[id]/+page.svelte';

type MountedComponent = ReturnType<typeof mount>;

const pipelineSettings = {
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
	backtest_fee_bps: 4.5,
	backtest_slippage_bps: 2,
	created_at: '2026-04-01T00:00:00Z',
	created_by: 'brain',
};

const pipelineThresholds = {
	quick_screen: {},
	gauntlet: {},
	paper: {},
	retirement: {},
	decay: {},
};

const gauntletStatus = {
	ok: true,
	strategy_id: 'S0001',
	workflow_id: null,
	workflow_status: null,
	current_step: null,
	stage: 'gauntlet',
	status: 'gauntlet',
	composite_robustness_score: 50,
	min_robustness_score: 60,
	tests: {
		walk_forward: { result_id: null, status: 'not_started', verdict: null },
		monte_carlo: { result_id: null, status: 'not_started', verdict: null },
		parameter_jitter: { result_id: null, status: 'not_started', verdict: null },
		cost_stress: { result_id: null, status: 'not_started', verdict: null },
		regime_split: { result_id: null, status: 'not_started', verdict: null },
	},
	tests_completed: 0,
	tests_passed: 0,
	tests_total: 5,
	required_tests: ['walk_forward', 'parameter_jitter', 'cost_stress'],
	missing_required: ['walk_forward', 'parameter_jitter', 'cost_stress'],
	ready_for_paper: false,
};

function buildHistoryItem(
	resultId: string,
	overrides: Record<string, unknown> = {},
): Record<string, unknown> {
	const metricOverrides =
		overrides.metrics && typeof overrides.metrics === 'object' ? overrides.metrics as Record<string, unknown> : {};
	const configOverrides =
		overrides.config && typeof overrides.config === 'object' ? overrides.config as Record<string, unknown> : {};
	const { metrics: _ignoredMetrics, config: _ignoredConfig, ...restOverrides } = overrides;
	return {
		result_id: resultId,
		result_type: 'backtest',
		created_at: '2026-03-11T22:14:15Z',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		start_date: '2025-03-11T00:00:00Z',
		end_date: '2026-03-11T00:00:00Z',
		metrics: {
			annualized_return_pct: 7.59,
			total_return_pct: 5.22,
			sharpe_ratio: 0.4,
			max_drawdown_pct: 27.11,
			win_rate: 48.6,
			total_trades: 15,
			profit_factor: 1.12,
			...metricOverrides,
		},
		config: {
			start: '2025-03-11T00:00:00Z',
			end: '2026-03-11T00:00:00Z',
			params: {
				fast: 12,
				slow: 26,
				signal: 9,
			},
			...configOverrides,
		},
		...restOverrides,
	};
}

function buildContainer(
	backtestIds: string[],
	options: {
		optimizations?: Record<string, unknown>[];
		params?: Record<string, unknown>;
		strategyName?: string;
		strategyType?: string;
		configurationType?: string;
	} = {},
): Record<string, unknown> {
	const backtests = backtestIds.map((resultId) => buildHistoryItem(resultId));
	const params = options.params ?? {
		fast: 12,
		slow: 26,
		signal: 9,
	};
	return {
		strategy: {
			id: 'S0001',
			name: options.strategyName ?? 'BTC-MACD-S0001',
			hypothesis_id: 'HYP-001',
			hypothesis_display_id: 'H00001',
			state: 'backtesting',
			type: options.strategyType ?? 'btc_macd_s0001',
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
			params,
			type: options.configurationType ?? 'manual',
			owner: 'brain',
			stage: 'backtesting',
		},
		history: {
			all: backtests,
			backtests,
			optimizations: options.optimizations ?? [],
			walk_forward: [],
		},
		execution: {
			trades: [],
			positions: [],
		},
		events: [],
	};
}

function buildResult(
	resultId: string,
	overrides: Record<string, unknown> = {},
): Record<string, unknown> {
	const metricOverrides =
		overrides.metrics && typeof overrides.metrics === 'object' ? overrides.metrics as Record<string, unknown> : {};
	const configOverrides =
		overrides.config && typeof overrides.config === 'object' ? overrides.config as Record<string, unknown> : {};
	const { metrics: _ignoredMetrics, config: _ignoredConfig, ...restOverrides } = overrides;
	return {
		id: resultId,
		result_type: 'backtest',
		strategy_name: 'BTC-MACD-S0001',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		created_at: '2026-03-11T22:14:15Z',
		metrics: {
			annualized_return_pct: 7.59,
			total_return_pct: 5.22,
			sharpe_ratio: 0.4,
			max_drawdown_pct: 27.11,
			win_rate: 48.6,
			total_trades: 15,
			profit_factor: 1.12,
			...metricOverrides,
		},
		config: {
			start: '2025-03-11T00:00:00Z',
			end: '2026-03-11T00:00:00Z',
			params: {
				fast: 12,
				slow: 26,
				signal: 9,
			},
			...configOverrides,
		},
		...restOverrides,
	};
}

function buildRuleBlobDefinition(): Record<string, unknown> {
	return {
		indicators: [
			{
				name: 'MACD_12_26_9',
				type: 'macd',
				params: {
					fast: 12,
					slow: 26,
					signal: 9,
				},
			},
		],
		entry_conditions: [
			{
				condition: 'crosses_above',
				left: 'MACD_12_26_9',
				right: 'MACDs_12_26_9',
			},
		],
		exit_conditions: [
			{
				condition: 'crosses_below',
				left: 'MACD_12_26_9',
				right: 'MACDs_12_26_9',
			},
		],
	};
}

function buildPrebuiltStrategies(
	parameters: Record<string, unknown> = {
		fast: { type: 'number', default: 12, min: 1, max: 50, step: 1 },
		slow: { type: 'number', default: 26, min: 1, max: 100, step: 1 },
		signal: { type: 'number', default: 9, min: 1, max: 20, step: 1 },
		threshold: { type: 'number', default: 0.75, min: 0, max: 1, step: 0.05 },
	},
	options: {
		name?: string;
		api_name?: string;
		description?: string;
	} = {},
): Record<string, unknown> {
	return {
		strategies: [
			{
				name: options.name ?? 'BTC-MACD-S0001',
				api_name: options.api_name ?? 'BTC-MACD-S0001',
				version: '1.0.0',
				description: options.description ?? 'Prebuilt BTC MACD strategy',
				parameters,
			},
		],
	};
}

function buildChartContext(
	resultId: string,
	overrides: Record<string, unknown> = {},
): Record<string, unknown> {
	return {
		result_id: resultId,
		source: 'artifact',
		bars: [
			{ timestamp: '2026-03-10T00:00:00Z', open: 100, high: 105, low: 98, close: 103, volume: 1200 },
			{ timestamp: '2026-03-10T01:00:00Z', open: 103, high: 107, low: 101, close: 106, volume: 1320 },
		],
		entry_markers: [{ timestamp: '2026-03-10T00:00:00Z', price: 101 }],
		exit_markers: [{ timestamp: '2026-03-10T01:00:00Z', price: 106 }],
		main_indicators: [
			{
				name: 'EMA Fast',
				color: '#22d3ee',
				data: [
					{ timestamp: '2026-03-10T00:00:00Z', value: 100.5 },
					{ timestamp: '2026-03-10T01:00:00Z', value: 102.5 },
				],
			},
		],
		sub_indicators: [
			{
				name: 'MACD',
				color: '#f59e0b',
				data: [
					{ timestamp: '2026-03-10T00:00:00Z', value: 0.2 },
					{ timestamp: '2026-03-10T01:00:00Z', value: 0.6 },
				],
			},
		],
		strategy_name: 'BTC-MACD-S0001',
		strategy_meta: 'BTC/USDT / 1h',
		strategy_params: {
			fast: 12,
			slow: 26,
			signal: 9,
		},
		warnings: [],
		...overrides,
	};
}

function deferred<T>(): {
	promise: Promise<T>;
	resolve: (value: T) => void;
	reject: (reason?: unknown) => void;
} {
	let resolve!: (value: T) => void;
	let reject!: (reason?: unknown) => void;
	const promise = new Promise<T>((resolvePromise, rejectPromise) => {
		resolve = resolvePromise;
		reject = rejectPromise;
	});
	return { promise, resolve, reject };
}

async function flush(): Promise<void> {
	await Promise.resolve();
	await tick();
	await Promise.resolve();
	await tick();
}

async function waitForCondition(
	predicate: () => boolean,
	options: { attempts?: number } = {},
): Promise<void> {
	const attempts = options.attempts ?? 18;
	for (let index = 0; index < attempts; index += 1) {
		if (predicate()) {
			return;
		}
		await flush();
	}
	throw new Error('Timed out waiting for expected UI state.');
}

function click(element: Element | null): void {
	expect(element).not.toBeNull();
	element?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
}

function setInputValue(element: HTMLInputElement | null, value: string): void {
	expect(element).not.toBeNull();
	if (!element) return;
	element.value = value;
	element.dispatchEvent(new Event('input', { bubbles: true }));
}

function setSelectValue(element: HTMLSelectElement | null, value: string): void {
	expect(element).not.toBeNull();
	if (!element) return;
	element.value = value;
	element.dispatchEvent(new Event('change', { bubbles: true }));
}

function clickButtonByText(target: HTMLDivElement, label: string): void {
	const buttons = Array.from(target.querySelectorAll('button'));
	const button = buttons.find((candidate) => (candidate.textContent ?? '').includes(label)) ?? null;
	click(button);
}

function clickByTestId(target: HTMLDivElement, testId: string): void {
	click(target.querySelector(`[data-testid="${testId}"]`));
}

async function openBacktestHistory(target: HTMLDivElement): Promise<void> {
	await waitForCondition(() => target.querySelector('[data-testid="strategy-tab-backtests"]') !== null);
	clickByTestId(target, 'strategy-tab-backtests');
	await waitForCondition(() => target.querySelector('[data-testid^="backtest-row-"]') !== null);
}

async function openOptimizationTab(target: HTMLDivElement): Promise<void> {
	await waitForCondition(() => target.querySelector('[data-testid="strategy-tab-optimizations"]') !== null);
	clickByTestId(target, 'strategy-tab-optimizations');
	await waitForCondition(() => target.textContent?.includes('Run Optimization') ?? false);
}

async function openRobustnessTab(target: HTMLDivElement): Promise<void> {
	await waitForCondition(() => target.querySelector('[data-testid="strategy-tab-robustness"]') !== null);
	clickByTestId(target, 'strategy-tab-robustness');
	await waitForCondition(() => target.textContent?.includes('Robustness Runners') ?? false);
}

async function openOptimizationHistory(target: HTMLDivElement): Promise<void> {
	await openOptimizationTab(target);
	await waitForCondition(() => target.querySelector('[data-testid^="optimization-row-"]') !== null);
}

async function openGauntletParamsPanel(target: HTMLDivElement): Promise<void> {
	await waitForCondition(() => target.querySelector('[data-testid="strategy-tab-backtests"]') !== null);
	clickByTestId(target, 'strategy-tab-backtests');
	await waitForCondition(() => target.textContent?.includes('Gauntlet Parameters') ?? false);
}

async function waitForAddParamMetadata(target: HTMLDivElement): Promise<void> {
	await waitForCondition(() => {
		const addParamSelect = target.querySelector<HTMLSelectElement>('[data-testid="add-param-select"]');
		return addParamSelect !== null && addParamSelect.disabled === false;
	});
}

describe('/lab/strategy/[id] backtest history', () => {
	let target: HTMLDivElement;
	let app: MountedComponent | null = null;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
		appMocks.pageValue = {
			params: { id: 'S0001' },
			url: new URL('http://localhost/lab/strategy/S0001'),
		};
		apiMocks.deleteResult.mockReset();
		apiMocks.getDatasets.mockReset();
		apiMocks.getJob.mockReset();
		apiMocks.getPipelineSettings.mockReset();
		apiMocks.getResult.mockReset();
		apiMocks.getResultChartContext.mockReset();
		apiMocks.getPrebuiltStrategies.mockReset();
		apiMocks.getStrategyContainer.mockReset();
		apiMocks.submitBacktest.mockReset();
		apiMocks.submitOptimization.mockReset();
		apiMocks.getDatasets.mockResolvedValue([]);
		apiMocks.getPipelineSettings.mockResolvedValue(pipelineSettings);
		backtestingMocks.runCostStressRobustness.mockReset();
		backtestingMocks.runMonteCarloRobustness.mockReset();
		backtestingMocks.runParamJitterRobustness.mockReset();
		backtestingMocks.runRegimeSplitRobustness.mockReset();
		backtestingMocks.runWalkForwardRobustness.mockReset();
		backtestingMocks.getRobustnessResult.mockReset();
		backtestingMocks.getStrategyOpenPosition.mockReset();
		backtestingMocks.submitWalkForwardRobustness.mockReset();
		backtestingMocks.updateStrategyDefaultParams.mockReset();
		backtestingMocks.getStrategyOpenPosition.mockResolvedValue({
			has_open_position: false,
			count: 0,
			positions: [],
		});
		lifecycleMocks.getGauntletStatus.mockReset();
		lifecycleMocks.getPaperLiveReadiness.mockReset();
		lifecycleMocks.getPipelineConfig.mockReset();
		lifecycleMocks.getPromotionReadiness.mockReset();
		lifecycleMocks.getStrategyExecutionGrowth.mockReset();
		lifecycleMocks.runTimeframeSweep.mockReset();
		lifecycleMocks.getGauntletStatus.mockResolvedValue(gauntletStatus);
		lifecycleMocks.getPipelineConfig.mockResolvedValue(pipelineThresholds);
		lifecycleMocks.getPromotionReadiness.mockResolvedValue({
			ready: false,
			strategy_id: 'S0001',
			steps: [],
		});
		lifecycleMocks.getPaperLiveReadiness.mockResolvedValue({
			ready: false,
			strategy_id: 'S0001',
			steps: [],
		});
		lifecycleMocks.runTimeframeSweep.mockResolvedValue({
			ok: true,
			strategy_id: 'S0001',
			submitted: [],
			skipped: [],
			total_timeframes: 0,
		});
		toastMocks.addToast.mockReset();
		toastMocks.trackProcess.mockReset();
		toastMocks.trackedProcesses.set([]);
		window.confirm = vi.fn(() => true);
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
		vi.clearAllMocks();
	});

	it('loads chart context when a backtest row is selected', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickByTestId(target, 'backtest-row-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="selected-result-chart"]') !== null);

		expect(apiMocks.getResult).toHaveBeenCalledWith('B1001');
		expect(apiMocks.getResultChartContext).toHaveBeenCalledWith('B1001');
		expect(target.querySelector('[data-testid="selected-chart-source"]')?.textContent).toContain('Stored snapshot');
		expect(target.querySelector('[data-testid="selected-chart-bar-count"]')?.textContent).toContain('2 bars');
		expect(target.querySelector('[data-testid="selected-chart-view-mode"]')?.textContent).toContain('Full history');
		expect(target.textContent).toContain('BTC-MACD-S0001');
	});

	// Two runs with DISTINCT symbol/timeframe/window: B1001 (BTC/1h, drives the form on
	// load) and B2002 (ETH/4h, a different span). Shared by the sync + reset tests.
	function buildTwoRunContainer(): Record<string, unknown> {
		const container = buildContainer(['B1001']);
		(container.history as Record<string, unknown>).backtests = [
			buildHistoryItem('B1001', {
				symbol: 'BTC/USDT',
				timeframe: '1h',
				start_date: '2025-03-11T00:00:00Z',
				end_date: '2026-03-11T00:00:00Z',
				config: { start: '2025-03-11T00:00:00Z', end: '2026-03-11T00:00:00Z', params: { fast: 12, slow: 26, signal: 9 } },
			}),
			buildHistoryItem('B2002', {
				symbol: 'ETH/USDT',
				timeframe: '4h',
				start_date: '2024-01-15T00:00:00Z',
				end_date: '2024-07-15T00:00:00Z',
				config: { start: '2024-01-15T00:00:00Z', end: '2024-07-15T00:00:00Z', params: { fast: 8, slow: 21, signal: 5 } },
			}),
		];
		return container;
	}

	it('syncs the backtest symbol, timeframe and window to the run when a gauntlet history row is clicked', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildTwoRunContainer());
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		const symbolInput = () => target.querySelector('#container-backtest-symbol') as HTMLInputElement | null;
		const timeframeInput = () => target.querySelector('#container-backtest-timeframe') as HTMLSelectElement | null;
		const startInput = () => target.querySelector('#container-backtest-start') as HTMLInputElement | null;
		const endInput = () => target.querySelector('#container-backtest-end') as HTMLInputElement | null;
		// On load the form takes the container default (FIRST run's) context.
		expect(symbolInput()?.value).toBe('BTC/USDT');
		expect(timeframeInput()?.value).toBe('1h');
		expect(startInput()?.value).toBe('2025-03-11');
		expect(endInput()?.value).toBe('2026-03-11');

		// Clicking the second row re-points symbol/timeframe/window at THAT run.
		clickByTestId(target, 'backtest-row-B2002');
		await waitForCondition(() => startInput()?.value === '2024-01-15');
		expect(symbolInput()?.value).toBe('ETH/USDT');
		expect(timeframeInput()?.value).toBe('4h');
		expect(startInput()?.value).toBe('2024-01-15');
		expect(endInput()?.value).toBe('2024-07-15');
	});

	it('restores the container default symbol/timeframe/window when Reset to Defaults is clicked after selecting a run', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildTwoRunContainer());
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		const symbolInput = () => target.querySelector('#container-backtest-symbol') as HTMLInputElement | null;
		const timeframeInput = () => target.querySelector('#container-backtest-timeframe') as HTMLSelectElement | null;
		const startInput = () => target.querySelector('#container-backtest-start') as HTMLInputElement | null;
		const endInput = () => target.querySelector('#container-backtest-end') as HTMLInputElement | null;

		// Move the full context to the second run, then Reset to Defaults must restore the
		// container default (first run) symbol/timeframe/window — not leave it on the run.
		clickByTestId(target, 'backtest-row-B2002');
		await waitForCondition(() => startInput()?.value === '2024-01-15');

		clickByTestId(target, 'backtest-params-reset');
		await waitForCondition(() => startInput()?.value === '2025-03-11');
		expect(symbolInput()?.value).toBe('BTC/USDT');
		expect(timeframeInput()?.value).toBe('1h');
		expect(startInput()?.value).toBe('2025-03-11');
		expect(endInput()?.value).toBe('2026-03-11');
	});

	it('opens TradingView Pine in a copyable script panel', async () => {
		const writeText = vi.fn().mockResolvedValue(undefined);
		Object.defineProperty(navigator, 'clipboard', {
			value: { writeText },
			configurable: true,
		});
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('[data-testid="export-tradingview-button"]') !== null);

		clickByTestId(target, 'export-tradingview-button');
		await waitForCondition(() => target.querySelector('[data-testid="tradingview-export-dialog"]') !== null);

		const script = target.querySelector<HTMLTextAreaElement>('[data-testid="tradingview-export-script"]');
		expect(script?.value).toContain('//@version=6');
		expect(script?.value).toContain('fast_len = input.int(12, "MACD fast length"');
		expect(target.querySelector('[data-testid="tradingview-export-dialog"]')?.textContent).toContain('TradingView Pine Strategy');

		clickByTestId(target, 'copy-tradingview-script');
		await waitForCondition(() => writeText.mock.calls.length > 0);

		expect(writeText).toHaveBeenCalledWith(expect.stringContaining('strategy('));
		await waitForCondition(() => target.textContent?.includes('Copied') ?? false);
		expect(target.textContent).toContain('Copied');
	});

	it('shows result details while chart context is still loading', async () => {
		const chartRequest = deferred<Record<string, unknown>>();
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(() => chartRequest.promise);

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickByTestId(target, 'backtest-row-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="selected-result-chart-loading"]') !== null);

		expect(target.textContent).toContain('Trade chart');
		expect(target.textContent).not.toContain('Loading result details...');
		expect(target.querySelector('[data-testid="selected-chart-loading-chip"]')).not.toBeNull();

		chartRequest.resolve(buildChartContext('B1001'));
		await waitForCondition(() => target.querySelector('[data-testid="selected-result-chart"]') !== null);

		expect(target.querySelector('[data-testid="selected-chart-source"]')?.textContent).toContain('Stored snapshot');
	});

	it('normalizes ratio-based selected result metrics from live API payloads', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		apiMocks.getResult.mockImplementation(async (resultId: string) =>
			buildResult(resultId, {
				metrics: {
					annualized_return_pct: 4.57535,
					total_return_pct: undefined,
					total_return: 1.27757,
					sharpe_ratio: 2.937,
					max_drawdown_pct: undefined,
					max_drawdown: 0.17368,
					win_rate: 0.3991,
					total_trades: 223,
					profit_factor: 1.507,
				},
			}),
		);
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickByTestId(target, 'backtest-row-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="selected-result-total-return"]') !== null);

		expect(target.querySelector('[data-testid="selected-result-cagr"]')?.textContent).toContain('457.54%');
		expect(target.querySelector('[data-testid="selected-result-total-return"]')?.textContent).toContain('127.76%');
		expect(target.querySelector('[data-testid="selected-result-max-drawdown"]')?.textContent).toContain('17.37%');
		expect(target.querySelector('[data-testid="selected-result-win-rate"]')?.textContent).toContain('39.91%');
	});

	it('preserves percent-point metrics for legacy history and selected results', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		expect(target.querySelector('[data-testid="backtest-row-B1001"]')?.textContent).not.toContain('522.00%');

		clickByTestId(target, 'backtest-row-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="selected-result-total-return"]') !== null);

		expect(target.querySelector('[data-testid="selected-result-cagr"]')?.textContent).toContain('7.59%');
		expect(target.querySelector('[data-testid="selected-result-total-return"]')?.textContent).toContain('5.22%');
		expect(target.querySelector('[data-testid="selected-result-max-drawdown"]')?.textContent).toContain('27.11%');
		expect(target.querySelector('[data-testid="selected-result-win-rate"]')?.textContent).toContain('48.60%');
	});

	it('shows failed optimization rows as failed instead of fake zero-result runs', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(
			buildContainer([], {
				optimizations: [
					buildHistoryItem('OPT1001', {
						result_type: 'optimization',
						metrics: {},
						config: {
							status: 'failed',
							error: 'Grid search timed out after 300s',
							n_trials: 100,
							start: '2022-03-14T00:00:00Z',
							end: '2026-03-13T00:00:00Z',
						},
					}),
				],
			}),
		);
		apiMocks.getResult.mockResolvedValue(
			buildResult('OPT1001', {
				result_type: 'optimization',
				status: 'failed',
				error: 'Grid search timed out after 300s',
				metrics: {
					status: 'failed',
					error: 'Grid search timed out after 300s',
				},
				config: {
					status: 'failed',
					error: 'Grid search timed out after 300s',
					n_trials: 100,
					start: '2022-03-14T00:00:00Z',
					end: '2026-03-13T00:00:00Z',
				},
			}),
		);

		app = mount(StrategyDetailPage, { target });
		await openOptimizationHistory(target);

		const row = target.querySelector('[data-testid="optimization-row-OPT1001"]');
		expect(row?.textContent).toContain('Failed');
		expect(row?.textContent).toContain('Grid search timed out after 300s');
		expect(row?.textContent).toContain('100');
		expect(row?.textContent).not.toContain('0.00%');

		clickByTestId(target, 'optimization-row-OPT1001');
		await waitForCondition(() => target.querySelector('[data-testid="selected-result-status-banner"]') !== null);

		expect(apiMocks.getResultChartContext).not.toHaveBeenCalled();
		expect(target.querySelector('[data-testid="selected-result-status-badge"]')?.textContent).toContain('Failed');
		expect(target.querySelector('[data-testid="selected-result-status-banner"]')?.textContent).toContain('failed');
		expect(target.querySelector('[data-testid="selected-result-error-detail"]')?.textContent).toContain('Grid search timed out after 300s');
		expect(target.querySelector('[data-testid="selected-result-total-return"]')).toBeNull();
		expect(target.textContent).not.toContain('Loading result details...');
	});

	it('shows numeric optimization parameter controls (including zero-valued) and skips non-numeric params', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(
			buildContainer([], {
				params: {
					fast: 12,
					slow: 26.5,
					signal: 9,
					enabled: true,
					mode: 'trend',
					meta: { source: 'rule' },
					zero: 0,
					leverage: 0,
				},
			}),
		);

		app = mount(StrategyDetailPage, { target });
		await openOptimizationTab(target);

		expect(target.querySelector('[data-testid="optimization-params-panel"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="opt-param-select-fast"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="opt-param-select-slow"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="opt-param-select-signal"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="opt-param-select-enabled"]')).toBeNull();
		expect(target.querySelector('[data-testid="opt-param-select-mode"]')).toBeNull();
		expect(target.querySelector('[data-testid="opt-param-select-meta"]')).toBeNull();
		expect(target.querySelector('[data-testid="opt-param-select-leverage"]')).toBeNull();
		expect(target.querySelector('[data-testid="opt-exec-select-leverage"]')).not.toBeNull();
		// A numeric param defaulting to exactly 0 (e.g. an off-by-default threshold) is now
		// optimizable — its range is seeded via the step fallback rather than being hidden.
		expect(target.querySelector('[data-testid="opt-param-select-zero"]')).not.toBeNull();
	});

	it('selects and clears every optimization parameter via the select-all checkbox', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(
			buildContainer([], { params: { fast: 12, slow: 26, signal: 9 } }),
		);

		app = mount(StrategyDetailPage, { target });
		await openOptimizationTab(target);

		const panel = () => target.querySelector('[data-testid="optimization-params-panel"]') as HTMLElement | null;
		const selectAll = () => target.querySelector('[data-testid="opt-param-select-all"]') as HTMLInputElement | null;
		const paramBox = (key: string) =>
			target.querySelector(`[data-testid="opt-param-select-${key}"]`) as HTMLInputElement | null;

		// Nothing selected on load.
		expect(selectAll()).not.toBeNull();
		expect(selectAll()?.checked).toBe(false);
		expect(selectAll()?.indeterminate).toBe(false);
		expect(panel()?.textContent).toContain('0 selected');

		// Select all -> every param checkbox checked, header count reflects it.
		clickByTestId(target, 'opt-param-select-all');
		await waitForCondition(() => panel()?.textContent?.includes('3 selected') ?? false);
		expect(paramBox('fast')?.checked).toBe(true);
		expect(paramBox('slow')?.checked).toBe(true);
		expect(paramBox('signal')?.checked).toBe(true);
		expect(selectAll()?.checked).toBe(true);
		expect(selectAll()?.indeterminate).toBe(false);

		// Toggle select-all off -> everything cleared (clean toggle: it was fully checked).
		clickByTestId(target, 'opt-param-select-all');
		await waitForCondition(() => panel()?.textContent?.includes('0 selected') ?? false);
		expect(paramBox('fast')?.checked).toBe(false);
		expect(paramBox('slow')?.checked).toBe(false);
		expect(paramBox('signal')?.checked).toBe(false);
		expect(selectAll()?.checked).toBe(false);
		expect(selectAll()?.indeterminate).toBe(false);

		// Selecting a single param puts select-all into the indeterminate (some, not all) state.
		clickByTestId(target, 'opt-param-select-signal');
		await waitForCondition(() => selectAll()?.indeterminate === true);
		expect(selectAll()?.checked).toBe(false);
		expect(panel()?.textContent).toContain('1 selected');
	});

	it('submits selected optimization parameter ranges', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(
			buildContainer([], {
				params: {
					fast: 12,
					slow: 26,
					signal: 9,
					execution_profile: {
						initial_capital: 10000,
						fee_bps: 10,
						slippage_bps: 5,
						leverage: 2,
						sizing_mode: 'fraction',
						risk_per_trade: 0.01,
						stop_loss_pct: 2,
					},
				},
			}),
		);
		apiMocks.submitOptimization.mockResolvedValue({ job_id: 'OPT-JOB-1', status: 'succeeded' });

		app = mount(StrategyDetailPage, { target });
		await openOptimizationTab(target);

		setSelectValue(target.querySelector<HTMLSelectElement>('#container-opt-timeframe'), '4h');
		setInputValue(target.querySelector<HTMLInputElement>('#container-opt-start'), '2025-01-15');
		setInputValue(target.querySelector<HTMLInputElement>('#container-opt-end'), '2025-03-15');
		setSelectValue(target.querySelector<HTMLSelectElement>('#container-opt-objective'), 'total_return_pct');
		setInputValue(target.querySelector<HTMLInputElement>('#container-opt-trials'), '25');

		clickByTestId(target, 'opt-param-select-fast');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-min-fast"]'), '10');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-max-fast"]'), '20');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-step-fast"]'), '2');
		clickByTestId(target, 'opt-param-select-signal');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-min-signal"]'), '5');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-max-signal"]'), '11');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-step-signal"]'), '1');
		await waitForCondition(() => target.querySelector('[data-testid="opt-exec-select-leverage"]') !== null);
		clickByTestId(target, 'opt-exec-select-leverage');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-min-leverage"]'), '1');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-max-leverage"]'), '3');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-step-leverage"]'), '1');
		clickByTestId(target, 'opt-exec-select-stop_loss_pct');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-min-stop_loss_pct"]'), '1');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-max-stop_loss_pct"]'), '4');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-step-stop_loss_pct"]'), '1');
		await flush();

		clickButtonByText(target, 'Run Optimization');
		await waitForCondition(() => apiMocks.submitOptimization.mock.calls.length > 0);

		expect(apiMocks.submitOptimization).toHaveBeenCalledWith(expect.objectContaining({
			strategy_id: 'S0001',
			strategy_name: 'BTC-MACD-S0001',
			symbol: 'BTC/USDT',
			timeframe: '4h',
			start: '2025-01-15T00:00:00.000Z',
			end: '2025-03-15T00:00:00.000Z',
			objective: 'total_return_pct',
			n_trials: 25,
			parameter_ranges: {
				fast: { min: 10, max: 20, step: 2 },
				signal: { min: 5, max: 11, step: 1 },
			},
			execution_parameter_ranges: {
				leverage: { min: 1, max: 3, step: 1 },
				stop_loss_pct: { min: 1, max: 4, step: 1 },
			},
			execution_profile: expect.objectContaining({
				initial_capital: 10000,
				fee_bps: 10,
				slippage_bps: 5,
				leverage: 2,
				sizing_mode: 'fraction',
				risk_per_trade: 0.01,
				stop_loss_pct: 2,
			}),
			leverage: 2,
			stop_loss_pct: 2,
		}));
	});

	it('normalizes legacy zero leverage params before optimization submit', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(
			buildContainer([], {
				params: {
					fast: 12,
					slow: 26,
					signal: 9,
					leverage: 0,
				},
			}),
		);
		apiMocks.submitOptimization.mockResolvedValue({ job_id: 'OPT-JOB-LEGACY-LEV', status: 'succeeded' });

		app = mount(StrategyDetailPage, { target });
		await openOptimizationTab(target);

		clickByTestId(target, 'opt-param-select-fast');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-min-fast"]'), '10');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-max-fast"]'), '20');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-step-fast"]'), '2');
		await flush();

		clickButtonByText(target, 'Run Optimization');
		await waitForCondition(() => apiMocks.submitOptimization.mock.calls.length > 0);

		const payload = apiMocks.submitOptimization.mock.calls[0][0];
		expect(payload).toEqual(expect.objectContaining({
			strategy_id: 'S0001',
			leverage: 1,
			parameter_ranges: {
				fast: { min: 10, max: 20, step: 2 },
			},
			execution_profile: expect.objectContaining({
				leverage: 1,
			}),
		}));
		expect(payload.parameter_ranges).not.toHaveProperty('leverage');
		expect(target.textContent).not.toContain('Leverage must be greater than 0 and no more than 125.');
	});

	it('blocks invalid execution leverage ranges before optimization submit', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(
			buildContainer([], {
				params: {
					fast: 12,
					slow: 26,
					signal: 9,
				},
			}),
		);

		app = mount(StrategyDetailPage, { target });
		await openOptimizationTab(target);

		await waitForCondition(() => target.querySelector('[data-testid="opt-exec-select-leverage"]') !== null);
		clickByTestId(target, 'opt-exec-select-leverage');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-min-leverage"]'), '0');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-max-leverage"]'), '2');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-exec-step-leverage"]'), '1');
		await flush();

		clickButtonByText(target, 'Run Optimization');
		await flush();

		expect(apiMocks.submitOptimization).not.toHaveBeenCalled();
		expect(target.querySelector('[data-testid="opt-exec-error-leverage"]')?.textContent).toContain('leverage minimum must be greater than zero.');
	});

	it('blocks optimization submit when a selected range is invalid', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(
			buildContainer([], {
				params: {
					fast: 12,
					slow: 26,
					signal: 9,
				},
			}),
		);

		app = mount(StrategyDetailPage, { target });
		await openOptimizationTab(target);

		clickByTestId(target, 'opt-param-select-fast');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-min-fast"]'), '20');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-max-fast"]'), '10');
		setInputValue(target.querySelector<HTMLInputElement>('[data-testid="opt-param-step-fast"]'), '0');
		await flush();

		clickButtonByText(target, 'Run Optimization');
		await flush();

		expect(apiMocks.submitOptimization).not.toHaveBeenCalled();
		expect(target.querySelector('[data-testid="opt-param-error-fast"]')?.textContent).toContain('Minimum must be on or before maximum.');
		expect(target.textContent).toContain('Minimum must be on or before maximum.');
	});

	it('auto-selects the newest backtest after a successful run', async () => {
		apiMocks.getStrategyContainer
			.mockResolvedValueOnce(buildContainer(['B1001']))
			.mockResolvedValueOnce(buildContainer(['B2002', 'B1001']));
		apiMocks.submitBacktest.mockResolvedValue({ job_id: 'JOB-1', status: 'succeeded' });
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickButtonByText(target, 'Run the Gauntlet');
		await waitForCondition(() => apiMocks.getResultChartContext.mock.calls.some((call) => call[0] === 'B2002'));
		await waitForCondition(() => target.querySelector('[data-testid="selected-chart-source"]') !== null);

		expect(apiMocks.submitBacktest).toHaveBeenCalledWith(expect.objectContaining({
			strategy_id: 'S0001',
			strategy_name: 'BTC-MACD-S0001',
			symbol: 'BTC/USDT',
			timeframe: '1h',
			preserve_result: true,
			params: {
				fast: 12,
				slow: 26,
				signal: 9,
			},
		}));
		expect(target.textContent).toContain('B2002');
		expect(target.querySelector('[data-testid="selected-chart-source"]')?.textContent).toContain('Stored snapshot');
	});

	it('surfaces stock datasets as backtestable symbol suggestions', async () => {
		apiMocks.getStrategyContainer
			.mockResolvedValueOnce(buildContainer(['B1001']))
			.mockResolvedValueOnce(buildContainer(['B1001']));
		apiMocks.getDatasets.mockResolvedValue([
			{
				symbol: 'AAPL',
				timeframe: '1h',
				source: 'polygon',
				start_ts: '2025-03-11T00:00:00Z',
				end_ts: '2026-03-11T00:00:00Z',
				row_count: 4000,
				asset_class: 'stock',
				market_type: 'equity',
			},
		]);
		apiMocks.submitBacktest.mockResolvedValue({ job_id: 'JOB-STOCK-1', status: 'succeeded' });
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		await waitForCondition(() => target.querySelector('#container-backtest-symbol-suggestions option[value="AAPL"]') !== null);
		expect(target.textContent).toContain('Local backtest universe includes stocks / ETFs');

		const symbolInput = target.querySelector<HTMLInputElement>('#container-backtest-symbol');
		setInputValue(symbolInput, 'AAPL');
		await flush();

		clickButtonByText(target, 'Run the Gauntlet');
		await waitForCondition(() => apiMocks.submitBacktest.mock.calls.length > 0);

		expect(apiMocks.submitBacktest).toHaveBeenCalledWith(expect.objectContaining({
			symbol: 'AAPL',
			timeframe: '1h',
		}));
	});

	it('shows editable parameters on the backtest tab and submits the edited draft', async () => {
		apiMocks.getStrategyContainer
			.mockResolvedValueOnce(buildContainer(['B1001']))
			.mockResolvedValueOnce(buildContainer(['B1001']));
		apiMocks.submitBacktest.mockResolvedValue({ job_id: 'JOB-PARAMS-1', status: 'succeeded' });
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		expect(target.querySelector('[data-testid="backtest-parameter-panel"]')).not.toBeNull();
		expect(target.textContent).toContain('Gauntlet Parameters');

		const fastInput = Array.from(target.querySelectorAll<HTMLInputElement>('[data-testid="backtest-parameter-editor"] input[type="number"]'))
			.find((input) => input.value === '12') ?? null;
		setInputValue(fastInput, '15');
		await flush();

		clickButtonByText(target, 'Run the Gauntlet');
		await waitForCondition(() => apiMocks.submitBacktest.mock.calls.length > 0);

		expect(apiMocks.submitBacktest).toHaveBeenCalledWith(expect.objectContaining({
			strategy_id: 'S0001',
			params: {
				fast: 15,
				slow: 26,
				signal: 9,
			},
		}));
	});

	it('expands compact backtest parameter summaries from the overflow chip', async () => {
		const params = {
			alpha: 1,
			beta: 2,
			delta: 4,
			epsilon: 5,
			gamma: 3,
			leverage: 2,
			theta: 7,
			zeta: 6,
		};
		const container = buildContainer([], { params });
		(container.history as Record<string, unknown>).backtests = [
			buildHistoryItem('B1001', {
				config: { params },
			}),
		];
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		const summary = target.querySelector('[data-testid="backtest-param-summary-B1001"]');
		expect(summary?.textContent).toContain('alpha=1');
		expect(summary?.textContent).toContain('+4 more');
		expect(summary?.textContent).not.toContain('gamma=3');

		clickByTestId(target, 'backtest-param-overflow-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="backtest-param-summary-B1001"]')?.textContent?.includes('gamma=3') ?? false);
		expect(target.querySelector('[data-testid="backtest-param-overflow-B1001"]')?.textContent).toContain('Show less');

		clickByTestId(target, 'backtest-param-overflow-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="backtest-param-summary-B1001"]')?.textContent?.includes('+4 more') ?? false);
		expect(target.querySelector('[data-testid="backtest-param-summary-B1001"]')?.textContent).not.toContain('gamma=3');
	});

	it('pins the active/default Gauntlet run to the top of the history list regardless of sort', async () => {
		// The pinned run is the OLDER one, so the default created-desc sort would push
		// it below the newer run. withPinnedFirst must hoist it back to the top.
		const newer = buildHistoryItem('B_NEW', { created_at: '2026-05-01T00:00:00Z' });
		const older = buildHistoryItem('B_OLD', { created_at: '2026-01-01T00:00:00Z' });
		const container = buildContainer([]);
		(container.history as Record<string, unknown>).backtests = [newer, older];
		(container.history as Record<string, unknown>).all = [newer, older];
		(container.strategy as Record<string, unknown>).pinned_backtest_id = 'B_OLD';
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		const rows = Array.from(target.querySelectorAll('[data-testid^="backtest-row-"]'));
		expect(rows[0]?.getAttribute('data-testid')).toBe('backtest-row-B_OLD');
		// The active row carries the green inset border and an Active badge.
		expect(rows[0]?.getAttribute('class') ?? '').toContain('emerald');
		expect(rows[0]?.textContent).toContain('Active');
	});

	it('does not flag the active run\'s execution params as changed (no amber chips)', async () => {
		// The active/pinned run's stored params ARE the strategy default, so every chip —
		// including execution_profile and leverage that the alpha-param view strips —
		// must read as unchanged. A genuinely different value still highlights amber.
		const activeParams = {
			fast: 12,
			leverage: 2,
			execution_profile: { sizing_mode: 'fraction', risk_per_trade: 0.01 },
		};
		const container = buildContainer([], { params: activeParams });
		const active = buildHistoryItem('B_ACTIVE', { config: { params: activeParams } });
		const other = buildHistoryItem('B_OTHER', {
			config: { params: { fast: 12, leverage: 5 } },
		});
		(container.history as Record<string, unknown>).backtests = [active, other];
		(container.history as Record<string, unknown>).all = [active, other];
		(container.strategy as Record<string, unknown>).pinned_backtest_id = 'B_ACTIVE';
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		const activeChips = Array.from(
			target.querySelectorAll('[data-testid="backtest-param-summary-B_ACTIVE"] span'),
		);
		const leverageChip = activeChips.find((chip) => chip.textContent?.includes('leverage=2'));
		const profileChip = activeChips.find((chip) => chip.textContent?.includes('execution_profile'));
		expect(leverageChip).toBeTruthy();
		expect(leverageChip?.getAttribute('class') ?? '').not.toContain('yellow');
		expect(profileChip?.getAttribute('class') ?? '').not.toContain('yellow');

		// A run whose leverage differs from the default still flags the changed highlight.
		const otherChips = Array.from(
			target.querySelectorAll('[data-testid="backtest-param-summary-B_OTHER"] span'),
		);
		const otherLeverage = otherChips.find((chip) => chip.textContent?.includes('leverage=5'));
		expect(otherLeverage?.getAttribute('class') ?? '').toContain('yellow');
	});

	it('marks the Gauntlet Parameters card active (green) when no backtest run is pinned', async () => {
		// No pinned run => the manually-saved container defaults are the active params,
		// so the Gauntlet Parameters card surfaces a green Active treatment.
		const container = buildContainer(['B1001']);
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		const panel = target.querySelector('[data-testid="backtest-parameter-panel"]');
		expect(panel?.getAttribute('class') ?? '').toContain('emerald');
		expect(panel?.querySelector('summary')?.textContent).toContain('Active');
	});

	it('shows execution settings inside the Gauntlet Parameters panel', async () => {
		// The Gauntlet Parameters panel must render the shared execution-settings form
		// seeded from the saved execution_profile.
		const container = buildContainer(['B1001'], {
			params: {
				fast: 12,
				execution_profile: { sizing_mode: 'fraction', risk_per_trade: 0.02, leverage: 3 },
			},
		});
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await openGauntletParamsPanel(target);
		await waitForCondition(() => target.textContent?.includes('Gauntlet Parameters') ?? false);
		await waitForCondition(() => target.textContent?.includes('Execution Settings') ?? false);

		expect(target.textContent).toContain('Sizing Mode');
		// Fraction sizing reveals the Risk Per Trade field.
		expect(target.textContent).toContain('Risk Per Trade');
		// The Sizing Mode select reflects the saved execution_profile.
		const sizingSelect = Array.from(target.querySelectorAll<HTMLSelectElement>('select')).find(
			(select) => Array.from(select.options).some((option) => option.value === 'fraction'),
		);
		expect(sizingSelect?.value).toBe('fraction');
	});

	it('does not show the params draft as permanently dirty when the strategy has a saved execution_profile', async () => {
		// Regression: execution_profile is persisted INSIDE params but must be
		// stripped from the alpha-param draft on BOTH sides (strategyParams and
		// paramsDraft). Otherwise a freshly-loaded strategy reads as permanently
		// "Unsaved"/"Draft has changes" (the Save button never disables).
		const container = buildContainer([], {
			params: { fast: 12, slow: 26, signal: 9, execution_profile: { sizing_mode: 'fraction', risk_per_trade: 0.02, stop_loss_pct: 2 } },
		});
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		// Open the Gauntlet section (no history rows needed) and reach the params pane
		// on its DEFAULT view — the exact state the user reported as stuck "Unsaved".
		await waitForCondition(() => target.querySelector('[data-testid="strategy-tab-backtests"]') !== null);
		clickByTestId(target, 'strategy-tab-backtests');
		await waitForCondition(() => target.querySelector('[data-testid="backtest-params-save"]') !== null);

		const save = target.querySelector('[data-testid="backtest-params-save"]') as HTMLButtonElement | null;
		expect(save).not.toBeNull();
		expect(save?.disabled).toBe(true); // a freshly-loaded saved profile is NOT dirty -> Save disabled
		expect(target.textContent).not.toContain('Draft has changes');
	});

	it('loads gauntlet execution settings from a selected history row and resets to defaults', async () => {
		const container = buildContainer([], {
			params: { fast: 12, slow: 26, signal: 9, leverage: 1 },
		});
		(container.history as Record<string, unknown>).backtests = [
			buildHistoryItem('B1001', {
				config: {
					params: { fast: 15, slow: 30, signal: 8, leverage: 2 },
					initial_capital: 25000,
					fee_bps: 8,
					slippage_bps: 4,
					leverage: 2,
					sizing_mode: 'fraction',
					risk_per_trade: 0.03,
					stop_loss_pct: 5,
					take_profit_pct: 11,
				},
			}),
		];
		apiMocks.getStrategyContainer
			.mockResolvedValueOnce(container)
			.mockResolvedValueOnce(container);
		apiMocks.submitBacktest.mockResolvedValue({ job_id: 'JOB-EXEC-1', status: 'succeeded' });
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickByTestId(target, 'backtest-row-B1001');
		await waitForCondition(() => target.textContent?.includes('Run B1001') ?? false);
		clickButtonByText(target, 'Run the Gauntlet');
		await waitForCondition(() => apiMocks.submitBacktest.mock.calls.length > 0);

		expect(apiMocks.submitBacktest).toHaveBeenCalledWith(expect.objectContaining({
			params: expect.objectContaining({ fast: 15, slow: 30, signal: 8 }),
			initial_capital: 25000,
			fee_bps: 8,
			slippage_bps: 4,
			leverage: 2,
			sizing_mode: 'fraction',
			risk_per_trade: 0.03,
			stop_loss_pct: 5,
			take_profit_pct: 11,
		}));

		await waitForCondition(() => apiMocks.getStrategyContainer.mock.calls.length >= 2);
		await waitForCondition(() => target.textContent?.includes('Run B1001') ?? false);
		apiMocks.submitBacktest.mockClear();
		clickByTestId(target, 'backtest-params-reset');
		await waitForCondition(() => target.textContent?.includes('Container defaults') ?? false);
		clickButtonByText(target, 'Run the Gauntlet');
		await waitForCondition(() => apiMocks.submitBacktest.mock.calls.length > 0);

		expect(apiMocks.submitBacktest).toHaveBeenCalledWith(expect.objectContaining({
			params: expect.objectContaining({ fast: 12, slow: 26, signal: 9 }),
			initial_capital: 10000,
			fee_bps: 4.5,
			slippage_bps: 2,
			leverage: 1,
			sizing_mode: 'full',
		}));
	});

	describe('Add Param controls', () => {
		beforeEach(() => {
			apiMocks.getPrebuiltStrategies.mockResolvedValue(buildPrebuiltStrategies());
		});

		it('matches prebuilt param metadata by stable strategy type for generated container names', async () => {
			apiMocks.getPrebuiltStrategies.mockResolvedValue(
				buildPrebuiltStrategies(
					{
						ema_length: { type: 'number', default: 45, min: 1, max: 100, step: 1 },
						roc_length: { type: 'number', default: 10, min: 1, max: 30, step: 1 },
						rsi_exit: { type: 'number', default: 84, min: 1, max: 100, step: 1 },
						use_adx_filter: { type: 'boolean', default: true },
					},
					{
						name: 'ETH-RSI_MOMENTUM-S7762062',
						api_name: 'rsi_momentum',
						description: 'Prebuilt RSI momentum strategy',
					},
				),
			);
			apiMocks.getStrategyContainer.mockResolvedValue(
				buildContainer(['B1001'], {
					strategyName: 'BTC-RSI_MOMENTUM-S00329',
					configurationType: 'rsi_momentum',
					params: {
						ema_length: 45,
						roc_length: 10,
					},
				}),
			);

			app = mount(StrategyDetailPage, { target });
			await openGauntletParamsPanel(target);
			await waitForCondition(() => target.querySelector('[data-testid="add-param-select"]') !== null);
			await waitForCondition(() => target.textContent?.includes('available from ETH-RSI_MOMENTUM-S7762062.') ?? false);

			const addParamSelect = target.querySelector<HTMLSelectElement>('[data-testid="add-param-select"]');
			expect(addParamSelect?.disabled).toBe(false);
			const optionValues = Array.from(addParamSelect?.options ?? []).map((option) => option.value);
			expect(optionValues).toEqual(expect.arrayContaining(['rsi_exit', 'use_adx_filter']));
		});

		it('filters the Add Param dropdown to supported params that are not already in the draft', async () => {
			apiMocks.getStrategyContainer.mockResolvedValue(
				buildContainer(['B1001'], {
					params: {
						fast: 12,
						custom_note: 'keep this draft note',
					},
				}),
			);

			app = mount(StrategyDetailPage, { target });
			await openGauntletParamsPanel(target);
			await waitForCondition(() => target.querySelector('[data-testid="add-param-select"]') !== null);
			await waitForAddParamMetadata(target);

			const addParamSelect = target.querySelector<HTMLSelectElement>('[data-testid="add-param-select"]');
			expect(addParamSelect).not.toBeNull();
			expect(addParamSelect?.disabled).toBe(false);
			const optionValues = Array.from(addParamSelect?.options ?? []).map((option) => option.value);
			expect(optionValues).toEqual(expect.arrayContaining(['slow', 'signal', 'threshold']));
			expect(optionValues).not.toContain('fast');
		});

		it('adds a selected param to the draft with its default value', async () => {
			apiMocks.getStrategyContainer.mockResolvedValue(
				buildContainer(['B1001'], {
					params: {
						fast: 12,
						custom_note: 'keep this draft note',
					},
				}),
			);

			app = mount(StrategyDetailPage, { target });
			await openGauntletParamsPanel(target);
			await waitForCondition(() => target.querySelector('[data-testid="add-param-select"]') !== null);
			await waitForAddParamMetadata(target);

			setSelectValue(target.querySelector<HTMLSelectElement>('[data-testid="add-param-select"]'), 'threshold');
			clickByTestId(target, 'add-param-button');
			await flush();

			const numericInputs = Array.from(target.querySelectorAll<HTMLInputElement>('input[type="number"]'));
			expect(numericInputs.some((input) => input.value === '0.75')).toBe(true);
		});

		it('sends expanded params when saving after adding a param', async () => {
			apiMocks.getStrategyContainer.mockResolvedValue(
				buildContainer(['B1001'], {
					params: {
						fast: 12,
						custom_note: 'keep this draft note',
					},
				}),
			);
			backtestingMocks.updateStrategyDefaultParams.mockResolvedValue({
				ok: true,
				strategy_id: 'S0001',
				params: {
					fast: 12,
					custom_note: 'keep this draft note',
					threshold: 0.75,
				},
			});

			app = mount(StrategyDetailPage, { target });
			await openGauntletParamsPanel(target);
			await waitForCondition(() => target.querySelector('[data-testid="add-param-select"]') !== null);
			await waitForAddParamMetadata(target);

			setSelectValue(target.querySelector<HTMLSelectElement>('[data-testid="add-param-select"]'), 'threshold');
			clickByTestId(target, 'add-param-button');
			await flush();

			clickButtonByText(target, 'Save');
			await waitForCondition(() => backtestingMocks.updateStrategyDefaultParams.mock.calls.length > 0);

			// Save now also persists the execution profile under params.execution_profile
			// (the canonical home the optimizer/gauntlet read), alongside the alpha params.
			expect(backtestingMocks.updateStrategyDefaultParams).toHaveBeenCalledWith('S0001', expect.objectContaining({
				fast: 12,
				custom_note: 'keep this draft note',
				threshold: 0.75,
				execution_profile: expect.any(Object),
			}), { pinnedBacktestId: null });
		});

		it('disables Add Param controls when the draft already contains every supported param', async () => {
			apiMocks.getStrategyContainer.mockResolvedValue(
				buildContainer(['B1001'], {
					params: {
						fast: 12,
						slow: 26,
						signal: 9,
						threshold: 0.75,
					},
				}),
			);

			app = mount(StrategyDetailPage, { target });
			await openGauntletParamsPanel(target);
			await waitForCondition(() => target.textContent?.includes('All supported params from') ?? false);

			const addParamSelect = target.querySelector<HTMLSelectElement>('[data-testid="add-param-select"]');
			expect(addParamSelect).not.toBeNull();
			expect(addParamSelect?.disabled).toBe(true);
			expect(Array.from(addParamSelect?.options ?? []).map((option) => option.value)).toEqual(['']);
		});

		it('shows the metadata-unavailable fallback state when prebuilt strategy metadata cannot be loaded', async () => {
			apiMocks.getPrebuiltStrategies.mockRejectedValueOnce(new Error('metadata unavailable'));
			apiMocks.getStrategyContainer.mockResolvedValue(
				buildContainer(['B1001'], {
					params: {
						fast: 12,
					},
				}),
			);

			app = mount(StrategyDetailPage, { target });
			await openGauntletParamsPanel(target);
			await waitForCondition(() => target.querySelector('[data-testid="add-param-select"]') !== null);

			const addParamSelect = target.querySelector<HTMLSelectElement>('[data-testid="add-param-select"]');
			expect(addParamSelect).not.toBeNull();
			expect(addParamSelect?.disabled).toBe(true);
			expect(Array.from(addParamSelect?.options ?? []).map((option) => option.value)).toEqual(['']);
			expect(target.textContent).toContain('No matching prebuilt strategy metadata was found for this container.');
			expect(target.querySelector('[data-testid="add-param-button"]')?.getAttribute('disabled')).not.toBeNull();
		});

		it('renders the container UI before prebuilt metadata finishes loading', async () => {
			const prebuiltRequest = deferred<Record<string, unknown>>();
			apiMocks.getPrebuiltStrategies.mockReturnValue(prebuiltRequest.promise);
			apiMocks.getStrategyContainer.mockResolvedValue(
				buildContainer(['B1001'], {
					params: {
						fast: 12,
					},
				}),
			);

			app = mount(StrategyDetailPage, { target });
			await openGauntletParamsPanel(target);
			await waitForCondition(() => target.textContent?.includes('Gauntlet Parameters') ?? false);

			expect(target.textContent).toContain('Gauntlet Parameters');
			expect(target.textContent).not.toContain('Loading container...');
			const addParamSelect = target.querySelector('[data-testid="add-param-select"]') as HTMLSelectElement | null;
			expect(addParamSelect).not.toBeNull();
			expect(addParamSelect?.disabled).toBe(true);

			prebuiltRequest.resolve(buildPrebuiltStrategies());
			await flush();
		});

		it('keeps the newest prebuilt metadata when successive container loads overlap', async () => {
			const staleMetadataRequest = deferred<Record<string, unknown>>();
			apiMocks.getPrebuiltStrategies
				.mockReturnValueOnce(staleMetadataRequest.promise)
				.mockResolvedValueOnce(buildPrebuiltStrategies({
					slow: { type: 'number', default: 26, min: 1, max: 100, step: 1 },
					signal: { type: 'number', default: 9, min: 1, max: 20, step: 1 },
				}));
			apiMocks.getStrategyContainer.mockResolvedValue(
				buildContainer(['B1001'], {
					params: {
						fast: 12,
					},
				}),
			);
			backtestingMocks.updateStrategyDefaultParams.mockResolvedValue({
				ok: true,
				strategy_id: 'S0001',
				params: {
					fast: 13,
				},
			});

			app = mount(StrategyDetailPage, { target });
			await openGauntletParamsPanel(target);
			await waitForCondition(() => target.textContent?.includes('Gauntlet Parameters') ?? false);

			const fastInput = target.querySelector<HTMLInputElement>('input[type="number"]');
			setInputValue(fastInput, '13');
			await flush();

			clickButtonByText(target, 'Save');
			await waitForCondition(() => apiMocks.getStrategyContainer.mock.calls.length >= 2);
			await waitForCondition(() => apiMocks.getPrebuiltStrategies.mock.calls.length >= 2);
			await waitForCondition(() => {
				const addParamSelect = target.querySelector<HTMLSelectElement>('[data-testid="add-param-select"]');
				return addParamSelect !== null && addParamSelect.disabled === false;
			});

			expect(Array.from(target.querySelectorAll('[data-testid="add-param-select"] option')).map((option) => (option as HTMLOptionElement).value)).toEqual(
				expect.arrayContaining(['slow', 'signal']),
			);
			expect(target.textContent).not.toContain('No matching prebuilt strategy metadata was found for this container.');

			staleMetadataRequest.resolve(buildPrebuiltStrategies({
				threshold: { type: 'number', default: 0.75, min: 0, max: 1, step: 0.05 },
			}));
			await flush();

			const finalOptionValues = Array.from(
				target.querySelectorAll<HTMLOptionElement>('[data-testid="add-param-select"] option'),
			).map((option) => option.value);
			expect(finalOptionValues).toEqual(expect.arrayContaining(['slow', 'signal']));
			expect(finalOptionValues).not.toContain('threshold');
		});
	});

	it('includes parsed definition_json when submitting container backtests', async () => {
		const definition = buildRuleBlobDefinition();
		const container = buildContainer(['B1001']);
		(container.strategy as Record<string, unknown>).definition_json = JSON.stringify(definition);
		(container.configuration as Record<string, unknown>).params = definition;

		apiMocks.getStrategyContainer
			.mockResolvedValueOnce(container)
			.mockResolvedValueOnce(container);
		apiMocks.submitBacktest.mockResolvedValue({ job_id: 'JOB-2', status: 'succeeded' });
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickButtonByText(target, 'Run the Gauntlet');
		await waitForCondition(() => apiMocks.submitBacktest.mock.calls.length > 0);

		expect(apiMocks.submitBacktest).toHaveBeenCalledWith(expect.objectContaining({
			strategy_id: 'S0001',
			definition_json: definition,
		}));
	});

	it('reruns a historical backtest with edited row parameters', async () => {
		apiMocks.getStrategyContainer
			.mockResolvedValueOnce(buildContainer(['B1001']))
			.mockResolvedValueOnce(buildContainer(['B1001']));
		apiMocks.submitBacktest.mockResolvedValue({ job_id: 'JOB-RERUN-1', status: 'succeeded' });
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickByTestId(target, 'edit-backtest-params-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="backtest-param-editor-B1001"]') !== null);

		const firstNumberInput = target.querySelector<HTMLInputElement>('[data-testid="backtest-param-editor-B1001"] input[type="number"]');
		setInputValue(firstNumberInput, '15');
		await flush();

		clickByTestId(target, 'rerun-backtest-params-B1001');
		await waitForCondition(() => apiMocks.submitBacktest.mock.calls.length > 0);

		expect(apiMocks.submitBacktest).toHaveBeenCalledWith(expect.objectContaining({
			strategy_id: 'S0001',
			strategy_name: 'BTC-MACD-S0001',
			symbol: 'BTC/USDT',
			timeframe: '1h',
			start: '2025-03-11T00:00:00Z',
			end: '2026-03-11T00:00:00Z',
			params: {
				fast: 15,
				slow: 26,
				signal: 9,
			},
		}));
	});

	it('merges optimization base params before setting defaults', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(
			buildContainer([], {
				optimizations: [
					buildHistoryItem('OPT2001', {
						result_type: 'optimization',
						metrics: {
							best_params: {
								fast: 15,
								signal: 7,
							},
						},
						config: {
							base_params: {
								fast: 12,
								slow: 26,
								signal: 9,
								regime: 200,
							},
							start: '2025-03-11T00:00:00Z',
							end: '2026-03-11T00:00:00Z',
						},
					}),
				],
			}),
		);
		apiMocks.getResult.mockResolvedValue(
			buildResult('OPT2001', {
				result_type: 'optimization',
				metrics: {
					best_params: {
						fast: 15,
						signal: 7,
					},
					total_return_pct: 9.1,
					annualized_return_pct: 12.4,
				},
				config: {
					base_params: {
						fast: 12,
						slow: 26,
						signal: 9,
						regime: 200,
					},
					start: '2025-03-11T00:00:00Z',
					end: '2026-03-11T00:00:00Z',
				},
			}),
		);

		app = mount(StrategyDetailPage, { target });
		await openOptimizationHistory(target);

		clickByTestId(target, 'optimization-row-OPT2001');
		await waitForCondition(() => target.textContent?.includes('Set As Default') ?? false);

		clickButtonByText(target, 'Set As Default');
		await waitForCondition(() => backtestingMocks.updateStrategyDefaultParams.mock.calls.length > 0);

		expect(backtestingMocks.updateStrategyDefaultParams).toHaveBeenCalledWith('S0001', {
			fast: 15,
			slow: 26,
			signal: 7,
			regime: 200,
		}, { pinnedBacktestId: null });
	});

	it('sets defaults from the current backtest row draft', async () => {
		apiMocks.getStrategyContainer
			.mockResolvedValueOnce(buildContainer(['B1001']))
			.mockResolvedValueOnce(buildContainer(['B1001']));
		backtestingMocks.updateStrategyDefaultParams.mockResolvedValue({
			ok: true,
			strategy_id: 'S0001',
			params: {
				fast: 15,
				slow: 26,
				signal: 9,
			},
		});

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickByTestId(target, 'edit-backtest-params-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="backtest-param-editor-B1001"]') !== null);

		const firstNumberInput = target.querySelector<HTMLInputElement>('[data-testid="backtest-param-editor-B1001"] input[type="number"]');
		setInputValue(firstNumberInput, '15');
		await flush();

		clickByTestId(target, 'set-default-backtest-params-B1001');
		await waitForCondition(() => backtestingMocks.updateStrategyDefaultParams.mock.calls.length > 0);

		expect(backtestingMocks.updateStrategyDefaultParams).toHaveBeenCalledWith('S0001', {
			fast: 15,
			slow: 26,
			signal: 9,
		}, { pinnedBacktestId: 'B1001' });
	});

	it('expands the selected robustness runner from gauntlet status tiles', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));

		app = mount(StrategyDetailPage, { target });
		await openRobustnessTab(target);
		await waitForCondition(() => target.querySelector('[data-testid="gauntlet-test-monte_carlo"]') !== null);

		// All five runner headers render as accordions; only the selected test starts expanded.
		expect(target.textContent).toContain('Walk-Forward Analysis');
		expect(target.textContent).toContain('Monte Carlo Simulation');
		expect(target.querySelector('[data-testid="runner-body-walk_forward"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="runner-body-monte_carlo"]')).toBeNull();
		expect(target.querySelector('[data-testid="runner-body-param_jitter"]')).toBeNull();

		clickByTestId(target, 'gauntlet-test-monte_carlo');
		await waitForCondition(() => target.querySelector('[data-testid="runner-body-monte_carlo"]') !== null);

		clickByTestId(target, 'gauntlet-test-parameter_jitter');
		await waitForCondition(() => target.querySelector('[data-testid="runner-body-param_jitter"]') !== null);
	});

	it('marks the gauntlet status tile with the completed runner verdict', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		backtestingMocks.submitWalkForwardRobustness.mockResolvedValue({
			job_id: 'JOB-WF-FAIL',
			status: 'running',
			result_id: 'WF-FAIL',
		});
		apiMocks.getJob.mockResolvedValue({
			id: 'JOB-WF-FAIL',
			type: 'walk_forward',
			status: 'succeeded',
			created_at: '2026-04-23T12:00:00Z',
			updated_at: '2026-04-23T12:01:00Z',
			result_id: 'WF-FAIL',
			strategy_id: 'S0001',
			symbol: 'BTC/USDT',
			timeframe: '1h',
			error: null,
		});
		backtestingMocks.getRobustnessResult.mockResolvedValue({
			result_id: 'WF-FAIL',
			strategy_id: 'S0001',
			result_type: 'walk_forward',
			symbol: 'BTC/USDT',
			timeframe: '1h',
			start_date: '2025-04-23T00:00:00Z',
			end_date: '2026-04-23T00:00:00Z',
			created_at: '2026-04-23T12:01:00Z',
			deleted_at: null,
			status: 'succeeded',
			error: null,
			metrics: { verdict: 'FAIL' },
			config: {
				status: 'succeeded',
				job_id: 'JOB-WF-FAIL',
				completed_at: '2026-04-23T12:01:00Z',
			},
			payload: {
				verdict: 'FAIL',
				avg_is_sharpe: 0,
				avg_oos_sharpe: 0,
				degradation: 1,
				aggregate_oos: { total_trades: 1 },
				splits: [],
			},
		});

		app = mount(StrategyDetailPage, { target });
		await openRobustnessTab(target);
		await waitForCondition(() => target.querySelector('[data-testid="gauntlet-test-verdict-walk_forward"]') !== null);
		expect(target.querySelector('[data-testid="gauntlet-test-verdict-walk_forward"]')?.textContent).toContain('OFF');

		clickButtonByText(target, 'Run WFA');
		await waitForCondition(() =>
			target.querySelector('[data-testid="gauntlet-test-verdict-walk_forward"]')?.textContent?.includes('FAIL') ?? false,
		);

		expect(target.querySelector('[data-testid="gauntlet-test-verdict-walk_forward"]')?.textContent).toContain('FAIL');
		expect(target.textContent).toContain('1 / 5 completed');
	});

	it('renders the strategy growth curve from closed execution trades on the Overview', async () => {
		const container = buildContainer(['B1001']);
		(container.execution as Record<string, unknown>).trades = [
			{
				id: 'T1',
				asset: 'BTC',
				direction: 'long',
				status: 'CLOSED',
				execution_type: 'paper_challenger',
				entry_price: 100,
				pnl: 120,
				pnl_usd: 120,
				pnl_pct: 1.2,
				opened_at: '2026-03-01T00:00:00Z',
				closed_at: '2026-03-02T00:00:00Z',
			},
			{
				id: 'T2',
				asset: 'BTC',
				direction: 'short',
				status: 'CLOSED',
				execution_type: 'paper_challenger',
				entry_price: 110,
				pnl: -45,
				pnl_usd: -45,
				pnl_pct: -0.4,
				opened_at: '2026-03-03T00:00:00Z',
				closed_at: '2026-03-04T00:00:00Z',
			},
			{
				id: 'T3',
				asset: 'BTC',
				direction: 'long',
				status: 'OPEN',
				execution_type: 'paper_challenger',
				entry_price: 111,
				pnl: null,
				opened_at: '2026-03-05T00:00:00Z',
				closed_at: null,
			},
		];
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('[data-testid="overview-growth-card"]') !== null);

		const card = target.querySelector('[data-testid="overview-growth-card"]');
		// The open trade is excluded; total = +120 - 45 = +$75.00 across 2 closed trades.
		expect(card?.textContent).toContain('2 closed trades');
		expect(card?.textContent).toContain('+$75.00');
		// All closed trades are paper -> the curve is the $10k paper book equity.
		expect(card?.textContent).toContain('Paper book equity');
		const chartStub = card?.querySelector('[data-testid="equity-chart-stub"]');
		expect(chartStub).not.toBeNull();
		// Anchor point at the earliest open + one point per closed trade.
		expect(chartStub?.getAttribute('data-points')).toBe('3');
		expect(chartStub?.getAttribute('data-drawdown')).toBe('yes');
	});

	it('shows the growth empty state when the strategy has no closed trades', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('[data-testid="overview-growth-card"]') !== null);

		expect(target.querySelector('[data-testid="overview-growth-card"]')?.textContent).toContain(
			'No closed paper/live trades yet',
		);
	});

	it('prefers the uncapped execution-growth endpoint over the container trade slice', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		lifecycleMocks.getStrategyExecutionGrowth.mockResolvedValue({
			ok: true,
			strategy_id: 'S0001',
			trades: [
				{ closed_at: '2026-01-02T00:00:00Z', opened_at: '2026-01-01T00:00:00Z', pnl: 50, execution_type: 'paper_challenger' },
				{ closed_at: '2026-01-04T00:00:00Z', opened_at: '2026-01-03T00:00:00Z', pnl: -20, execution_type: 'paper_challenger' },
				{ closed_at: '2026-01-06T00:00:00Z', opened_at: '2026-01-05T00:00:00Z', pnl: 30, execution_type: 'paper_challenger' },
			],
		});

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() =>
			target.querySelector('[data-testid="overview-growth-card"] [data-testid="equity-chart-stub"]') !== null,
		);

		const card = target.querySelector('[data-testid="overview-growth-card"]');
		expect(card?.textContent).toContain('3 closed trades');
		expect(card?.textContent).toContain('+$60.00');
		// Anchor point + one point per closed trade from the endpoint series.
		expect(card?.querySelector('[data-testid="equity-chart-stub"]')?.getAttribute('data-points')).toBe('4');
	});

	it('marks lifecycle events inside the growth window as chart annotations', async () => {
		const container = buildContainer(['B1001']);
		(container.execution as Record<string, unknown>).trades = [
			{
				id: 'T1', asset: 'BTC', direction: 'long', status: 'CLOSED', execution_type: 'paper_challenger',
				pnl: 120, pnl_usd: 120, opened_at: '2026-03-01T00:00:00Z', closed_at: '2026-03-02T00:00:00Z',
			},
			{
				id: 'T2', asset: 'BTC', direction: 'short', status: 'CLOSED', execution_type: 'paper_challenger',
				pnl: -45, pnl_usd: -45, opened_at: '2026-03-03T00:00:00Z', closed_at: '2026-03-04T00:00:00Z',
			},
		];
		(container.events as Record<string, unknown>[]) = [
			{
				id: 'E1', strategy_id: 'S0001', from_state: 'gauntlet', to_state: 'paper', actor: 'brain',
				reason: null, idempotency_key: null, created_at: '2026-03-03T12:00:00Z',
				owner_from: null, owner_to: null, details_json: null,
			},
		];
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() =>
			target.querySelector('[data-testid="overview-growth-card"] [data-testid="equity-chart-stub"]') !== null,
		);

		const chartStub = target.querySelector('[data-testid="overview-growth-card"] [data-testid="equity-chart-stub"]');
		expect(chartStub?.getAttribute('data-annotations')).toBe('1');
	});

	it('shows the Backtest↔Reality parity card from recorded slippage and cost drag', async () => {
		const container = buildContainer(['B1001']);
		(container.execution as Record<string, unknown>).trades = [
			{
				id: 'T1', asset: 'BTC', direction: 'long', status: 'CLOSED', execution_type: 'paper_challenger',
				pnl_usd: 100, pnl_pct: 0.052, net_pnl_pct: 0.05, leverage: 2,
				entry_slippage_bps: 6, exit_slippage_bps: 2,
				opened_at: '2026-03-01T00:00:00Z', closed_at: '2026-03-02T00:00:00Z',
			},
			{
				id: 'T2', asset: 'BTC', direction: 'long', status: 'CLOSED', execution_type: 'paper_challenger',
				pnl_usd: -40, pnl_pct: -0.02, net_pnl_pct: -0.022, leverage: 2,
				entry_slippage_bps: 2, exit_slippage_bps: -1,
				opened_at: '2026-03-03T00:00:00Z', closed_at: '2026-03-04T00:00:00Z',
			},
		];
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('[data-testid="overview-parity-metrics"]') !== null);

		const card = target.querySelector('[data-testid="overview-parity-card"]');
		// Avg entry slippage (6+2)/2 = +4.0 bps, exit (2-1)/2 = +0.5 bps.
		expect(card?.textContent).toContain('+4.0 bps');
		expect(card?.textContent).toContain('+0.5 bps');
		// Cost drag: ((0.052-0.05)+( -0.02 - -0.022))/2 = 0.002 → 0.200%.
		expect(card?.textContent).toContain('0.200%');
		expect(card?.textContent).toContain('avg leverage 2.0×');
	});

	it('marks stale gauntlet verdicts with a STALE badge and warning', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		lifecycleMocks.getGauntletStatus.mockResolvedValue({
			...gauntletStatus,
			tests: {
				...gauntletStatus.tests,
				walk_forward: { result_id: 'WF-1', status: 'passed', verdict: 'PASS', stale: true },
			},
		});

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('[data-testid="gauntlet-test-stale-walk_forward"]') !== null);

		expect(target.querySelector('[data-testid="gauntlet-test-stale-walk_forward"]')?.textContent).toContain('Stale');
		expect(target.querySelector('[data-testid="gauntlet-stale-warning"]')?.textContent).toContain('Params changed');
	});

	it('renders readiness progress bars from the numeric extra payload', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		lifecycleMocks.getPromotionReadiness.mockResolvedValue({
			ready: false,
			strategy_id: 'S0001',
			steps: [
				{
					name: 'validation_artifacts',
					status: 'failed',
					detail: 'Insufficient paper trades: 6/10',
					actionable: null,
					extra: { current: 6, threshold: 10, direction: 'gte', unit: 'trades' },
				},
			],
		});

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() =>
			target.querySelector('[data-testid="readiness-progress-validation_artifacts"]') !== null,
		);

		const progress = target.querySelector('[data-testid="readiness-progress-validation_artifacts"]');
		expect(progress?.textContent).toContain('6 trades');
		expect(progress?.textContent).toContain('target 10 trades');
	});

	it('shows rich closed/open trade views and manual close on the Execution tab', async () => {
		const container = buildContainer(['B1001']);
		(container.strategy as Record<string, unknown>).paper_session_id = 'PS-1';
		(container.execution as Record<string, unknown>).trades = [
			{
				id: 'T-CLOSED', asset: 'BTC', direction: 'long', status: 'CLOSED', execution_type: 'paper_challenger',
				fill_entry_price: 100.5, fill_exit_price: 105.9,
				pnl_usd: 120, pnl_pct: 0.0518, net_pnl_pct: 0.0512, fees_pct: 0.0018,
				entry_slippage_bps: 4.2, exit_slippage_bps: -1.1,
				signal_data: JSON.stringify({ close_reason: 'take_profit' }),
				opened_at: '2026-03-01T00:00:00Z', closed_at: '2026-03-02T00:00:00Z',
			},
			{
				id: 'T-OPEN', asset: 'BTC', direction: 'short', status: 'OPEN', execution_type: 'paper_challenger',
				entry_price: 100, size: 0.5, leverage: 3,
				signal_data: JSON.stringify({ stop_loss: 95.5, take_profit: 120.25 }),
				opened_at: '2026-03-05T00:00:00Z', closed_at: null,
			},
		];
		apiMocks.getStrategyContainer.mockResolvedValue(container);

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('[data-testid="strategy-tab-execution"]') !== null);
		clickByTestId(target, 'strategy-tab-execution');
		await waitForCondition(() => target.querySelector('[data-testid="execution-closed-trades"]') !== null);

		// Summary strip aggregates the closed trade.
		expect(target.querySelector('[data-testid="execution-summary-strip"]')?.textContent).toContain('+$120.00');
		// Fraction fields are scaled to percent for display.
		const closedTable = target.querySelector('[data-testid="execution-closed-trades"]');
		expect(closedTable?.textContent).toContain('5.12%');
		expect(closedTable?.textContent).toContain('0.180%');
		expect(closedTable?.textContent).toContain('+4.2 bps');
		expect(closedTable?.textContent).toContain('-1.1 bps');
		expect(closedTable?.textContent).toContain('take profit');
		// Open position shows the in-code stop/target from signal_data + close control.
		const openTable = target.querySelector('[data-testid="execution-open-trades"]');
		expect(openTable?.textContent).toContain('95.5000');
		expect(openTable?.textContent).toContain('120.2500');
		expect(target.querySelector('[data-testid="execution-close-position"]')).not.toBeNull();
	});

	it('compares two gauntlet runs with metric deltas and a param diff', async () => {
		const container = buildContainer(['B1001', 'B1002']);
		(container.history as Record<string, unknown>).backtests = [
			buildHistoryItem('B1001', { metrics: { sharpe_ratio: 0.4 }, config: { params: { fast: 12, slow: 26, signal: 9 } } }),
			buildHistoryItem('B1002', { metrics: { sharpe_ratio: 0.9 }, config: { params: { fast: 18, slow: 26, signal: 9 } } }),
		];
		apiMocks.getStrategyContainer.mockResolvedValue(container);
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await openBacktestHistory(target);

		clickByTestId(target, 'compare-select-B1001');
		await waitForCondition(() => target.querySelector('[data-testid="compare-hint"]') !== null);
		clickByTestId(target, 'compare-select-B1002');
		await waitForCondition(() => target.querySelector('[data-testid="run-compare-panel"]') !== null);

		const panel = target.querySelector('[data-testid="run-compare-panel"]');
		expect(panel?.textContent).toContain('A · B1001');
		expect(panel?.textContent).toContain('B · B1002');
		expect(target.querySelector('[data-testid="compare-metrics-table"]')).not.toBeNull();
		// Param diff: only `fast` differs.
		expect(panel?.textContent).toContain('1 changed');
		const diff = target.querySelector('[data-testid="compare-param-diff"]');
		expect(diff?.textContent).toContain('fast');
		expect(diff?.textContent).toContain('12');
		expect(diff?.textContent).toContain('18');
	});

	it('links back to the parent hypothesis when one is attached', async () => {
		apiMocks.getStrategyContainer.mockResolvedValue(buildContainer(['B1001']));
		apiMocks.getResult.mockImplementation(async (resultId: string) => buildResult(resultId));
		apiMocks.getResultChartContext.mockImplementation(async (resultId: string) => buildChartContext(resultId));

		app = mount(StrategyDetailPage, { target });
		await waitForCondition(() => target.querySelector('a[href="/hypotheses/H00001"]') !== null);

		const link = target.querySelector('a[href="/hypotheses/H00001"]');
		expect(link).not.toBeNull();
		expect(link?.textContent).toContain('Crucible');
		expect(link?.textContent).toContain('H00001');
	});
});
