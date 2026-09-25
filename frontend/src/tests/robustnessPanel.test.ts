import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, tick, unmount } from 'svelte';
import type { StrategyContainerHistoryItem } from '../lib/api';

const trackerMocks = vi.hoisted(() => {
	type Subscriber<T> = (value: T) => void;

	function createStore<T>(initialValue: T) {
		let value = initialValue;
		const subscribers = new Set<Subscriber<T>>();
		return {
			subscribe(callback: Subscriber<T>) {
				callback(value);
				subscribers.add(callback);
				return () => subscribers.delete(callback);
			},
			set(nextValue: T) {
				value = nextValue;
				for (const subscriber of subscribers) {
					subscriber(value);
				}
			},
			update(updater: (current: T) => T) {
				this.set(updater(value));
			},
			get() {
				return value;
			},
		};
	}

	const trackedProcesses = createStore<any[]>([]);
	const addToast = vi.fn();
	const trackProcess = vi.fn((id: string, type: string, label: string, href: string, data: Record<string, unknown>) => {
		const entry = {
			id,
			type,
			label,
			href,
			status: String(data.status ?? 'unknown'),
			data,
			addedAt: Date.now(),
			lastPoll: Date.now(),
		};
		trackedProcesses.update((current) => {
			const index = current.findIndex((item) => item.id === id && item.type === type);
			if (index >= 0) {
				const next = [...current];
				next[index] = entry;
				return next;
			}
			return [...current, entry];
		});
	});

	return { trackedProcesses, addToast, trackProcess };
});

const apiMocks = vi.hoisted(() => ({
	getJob: vi.fn(),
}));

const backtestingMocks = vi.hoisted(() => ({
	getHoldoutSummary: vi.fn(async () => ({ enabled: false, cutoff: null, state: 'off' })),
	submitHoldout: vi.fn(),
	getRobustnessResult: vi.fn(),
	submitCostStressRobustness: vi.fn(),
	submitMonteCarloRobustness: vi.fn(),
	submitParamJitterRobustness: vi.fn(),
	submitRegimeSplitRobustness: vi.fn(),
	submitWalkForwardRobustness: vi.fn(),
}));

vi.mock('$lib/stores/processTracker', () => ({
	addToast: trackerMocks.addToast,
	trackProcess: trackerMocks.trackProcess,
	trackedProcesses: trackerMocks.trackedProcesses,
}));

vi.mock('$lib/api', () => ({
	getJob: apiMocks.getJob,
}));

vi.mock('$lib/api/backtesting', () => ({
	getHoldoutSummary: backtestingMocks.getHoldoutSummary,
	submitHoldout: backtestingMocks.submitHoldout,
	getRobustnessResult: backtestingMocks.getRobustnessResult,
	submitCostStressRobustness: backtestingMocks.submitCostStressRobustness,
	submitMonteCarloRobustness: backtestingMocks.submitMonteCarloRobustness,
	submitParamJitterRobustness: backtestingMocks.submitParamJitterRobustness,
	submitRegimeSplitRobustness: backtestingMocks.submitRegimeSplitRobustness,
	submitWalkForwardRobustness: backtestingMocks.submitWalkForwardRobustness,
}));

import RobustnessPanel from '../lib/components/robustness/RobustnessPanel.svelte';

type MountedComponent = ReturnType<typeof mount>;

function buildBacktestHistoryItem(resultId = 'B1001'): StrategyContainerHistoryItem {
	return {
		result_id: resultId,
		strategy_id: 'S0001',
		result_type: 'backtest',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		created_at: '2026-03-14T12:00:00Z',
		metrics: {},
		config: {},
		start_date: null,
		end_date: null,
		deleted_at: null,
	};
}

function buildValidationHistoryItem(
	resultId = 'WF-1001',
	status = 'succeeded',
	overrides: Record<string, unknown> = {},
): StrategyContainerHistoryItem {
	const configOverrides =
		overrides.config && typeof overrides.config === 'object' ? overrides.config as Record<string, unknown> : {};
	const metricOverrides =
		overrides.metrics && typeof overrides.metrics === 'object' ? overrides.metrics as Record<string, unknown> : {};
	const { config: _ignoredConfig, metrics: _ignoredMetrics, ...restOverrides } = overrides;
	return {
		result_id: resultId,
		strategy_id: 'S0001',
		result_type: 'walk_forward',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		created_at: '2026-03-14T12:00:00Z',
		metrics: metricOverrides,
		config: {
			status,
			job_id: `JOB-${resultId}`,
			completed_at: '2026-03-14T12:05:00Z',
			...configOverrides,
		},
		start_date: '2025-03-14T00:00:00Z',
		end_date: '2026-03-14T00:00:00Z',
		deleted_at: null,
		...restOverrides,
	};
}

function buildPersistedWalkForwardResult(resultId = 'WF-1001'): Record<string, unknown> {
	return {
		result_id: resultId,
		strategy_id: 'S0001',
		result_type: 'walk_forward',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		start_date: '2025-03-14T00:00:00Z',
		end_date: '2026-03-14T00:00:00Z',
		created_at: '2026-03-14T12:05:00Z',
		deleted_at: null,
		status: 'succeeded',
		error: null,
		metrics: {
			verdict: 'PASS',
		},
		config: {
			status: 'succeeded',
			job_id: `JOB-${resultId}`,
			completed_at: '2026-03-14T12:05:00Z',
		},
		payload: {
			verdict: 'PASS',
			avg_is_sharpe: 1.45,
			avg_oos_sharpe: 0.91,
			degradation: 0.25,
			aggregate_oos: {
				total_trades: 18,
			},
			splits: [
				{
					split: 1,
					bars: 100,
					in_sample: { trades: 12, sharpe: 1.6 },
					out_of_sample: { trades: 6, sharpe: 0.9 },
				},
			],
		},
	};
}

function buildJob(jobId: string, resultId: string, status: string): Record<string, unknown> {
	return {
		id: jobId,
		type: 'walk_forward',
		status,
		created_at: '2026-03-14T12:00:00Z',
		updated_at: '2026-03-14T12:05:00Z',
		result_id: resultId,
		strategy_id: 'S0001',
		symbol: 'BTC/USDT',
		timeframe: '1h',
		error: null,
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

async function waitForCondition(predicate: () => boolean, attempts = 20): Promise<void> {
	for (let index = 0; index < attempts; index += 1) {
		if (predicate()) {
			return;
		}
		await flush();
	}
	throw new Error('Timed out waiting for expected robustness panel state.');
}

function clickButtonByText(target: HTMLDivElement, label: string): void {
	const button =
		Array.from(target.querySelectorAll('button')).find((candidate) => (candidate.textContent ?? '').includes(label)) ?? null;
	expect(button).not.toBeNull();
	button?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
}

describe('RobustnessPanel', () => {
	let target: HTMLDivElement;
	let app: MountedComponent | null = null;

	beforeEach(() => {
		target = document.createElement('div');
		document.body.appendChild(target);
		trackerMocks.trackedProcesses.set([]);
		trackerMocks.addToast.mockReset();
		trackerMocks.trackProcess.mockClear();
		apiMocks.getJob.mockReset();
		backtestingMocks.getRobustnessResult.mockReset();
		backtestingMocks.submitCostStressRobustness.mockReset();
		backtestingMocks.submitMonteCarloRobustness.mockReset();
		backtestingMocks.submitParamJitterRobustness.mockReset();
		backtestingMocks.submitRegimeSplitRobustness.mockReset();
		backtestingMocks.submitWalkForwardRobustness.mockReset();
	});

	afterEach(() => {
		if (app) {
			unmount(app);
			app = null;
		}
		target.remove();
		vi.clearAllMocks();
	});

	it('loads the latest persisted validation result from history on mount', async () => {
		backtestingMocks.getRobustnessResult.mockResolvedValue(buildPersistedWalkForwardResult('WF-HISTORY'));

		app = mount(RobustnessPanel, {
			target,
			props: {
				strategyId: 'S0001',
				backtestHistory: [buildBacktestHistoryItem()],
				validationHistory: [buildValidationHistoryItem('WF-HISTORY', 'succeeded')],
				defaultSymbol: 'BTC/USDT',
				defaultTimeframe: '1h',
				symbolSuggestions: ['BTC/USDT'],
			},
		});

		await waitForCondition(() => backtestingMocks.getRobustnessResult.mock.calls.length === 1);
		await waitForCondition(() => (target.textContent ?? '').includes('Avg OOS Sharpe'));

		expect(backtestingMocks.getRobustnessResult).toHaveBeenCalledWith('WF-HISTORY');
		expect(target.textContent).toContain('PASS');
		expect(target.textContent).toContain('0.910');
	});

	it('shows the out-of-sample baseline hurdle next to a walk-forward result', async () => {
		const persisted = buildPersistedWalkForwardResult('WF-HURDLE');
		(persisted.payload as Record<string, unknown>).baseline_hurdle = {
			status: 'fail',
			n_days: 310,
			alpha_pct: -6.4,
			alpha_t: -1.2,
			beta_market: 0.55,
			beta_trend: 0.1,
			sharpe: { strategy: 0.31, buy_hold: 0.62, trend: 0.44 },
			reasons: ['OOS alpha -6.4%/yr after removing buy-and-hold and the trend baseline is not above +0.0%/yr'],
			paper_mode: 'observe',
			live_mode: 'enforce',
		};
		backtestingMocks.getRobustnessResult.mockResolvedValue(persisted);

		app = mount(RobustnessPanel, {
			target,
			props: {
				strategyId: 'S0001',
				backtestHistory: [buildBacktestHistoryItem()],
				validationHistory: [buildValidationHistoryItem('WF-HURDLE', 'succeeded')],
				defaultSymbol: 'BTC/USDT',
				defaultTimeframe: '1h',
				symbolSuggestions: ['BTC/USDT'],
			},
		});

		await waitForCondition(() => target.querySelector('[data-testid="wf-baseline-hurdle"]') !== null);
		const panel = target.querySelector('[data-testid="wf-baseline-hurdle"]')?.textContent ?? '';
		expect(panel).toContain('NO ALPHA');
		expect(panel).toContain('-6.4%');
		expect(panel).toContain('->paper observed · ->live blocks');
		expect(panel).toContain('310 OOS days');
	});

	it('submits walk-forward runs, tracks the job, polls completion, and hydrates the persisted result', async () => {
		backtestingMocks.submitWalkForwardRobustness.mockResolvedValue({
			job_id: 'JOB-WF-QUEUE',
			status: 'queued',
			result_id: 'WF-QUEUE',
		});
		apiMocks.getJob.mockResolvedValue(buildJob('JOB-WF-QUEUE', 'WF-QUEUE', 'succeeded'));
		backtestingMocks.getRobustnessResult.mockResolvedValue(buildPersistedWalkForwardResult('WF-QUEUE'));

		app = mount(RobustnessPanel, {
			target,
			props: {
				strategyId: 'S0001',
				backtestHistory: [buildBacktestHistoryItem()],
				validationHistory: [],
				defaultSymbol: 'BTC/USDT',
				defaultTimeframe: '1h',
				symbolSuggestions: ['BTC/USDT'],
			},
		});

		clickButtonByText(target, 'Run WFA');

		await waitForCondition(() => trackerMocks.trackProcess.mock.calls.length > 0);
		await waitForCondition(() => apiMocks.getJob.mock.calls.length > 0);
		await waitForCondition(() => backtestingMocks.getRobustnessResult.mock.calls.some((call) => call[0] === 'WF-QUEUE'));
		await waitForCondition(() => (target.textContent ?? '').includes('25.0%'));

		expect(backtestingMocks.submitWalkForwardRobustness).toHaveBeenCalledWith(expect.objectContaining({
			strategy_id: 'S0001',
			symbol: 'BTC/USDT',
			timeframe: '1h',
		}));
		expect(trackerMocks.addToast).toHaveBeenCalledWith('Walk-Forward queued', 'info', '/lab/strategy/S0001');
		expect(target.textContent).toContain('PASS');
	});

	it('rehydrates a tracked running job after unmount and remount', async () => {
		const firstPoll = deferred<Record<string, unknown>>();
		backtestingMocks.submitWalkForwardRobustness.mockResolvedValue({
			job_id: 'JOB-WF-REMOUNT',
			status: 'queued',
			result_id: 'WF-REMOUNT',
		});
		apiMocks.getJob
			.mockImplementationOnce(() => firstPoll.promise)
			.mockResolvedValueOnce(buildJob('JOB-WF-REMOUNT', 'WF-REMOUNT', 'succeeded'));
		backtestingMocks.getRobustnessResult.mockResolvedValue(buildPersistedWalkForwardResult('WF-REMOUNT'));

		app = mount(RobustnessPanel, {
			target,
			props: {
				strategyId: 'S0001',
				backtestHistory: [buildBacktestHistoryItem()],
				validationHistory: [],
				defaultSymbol: 'BTC/USDT',
				defaultTimeframe: '1h',
				symbolSuggestions: ['BTC/USDT'],
			},
		});

		clickButtonByText(target, 'Run WFA');
		await waitForCondition(() => trackerMocks.trackProcess.mock.calls.length > 0);
		await waitForCondition(() => apiMocks.getJob.mock.calls.length === 1);

		unmount(app);
		app = null;
		target.innerHTML = '';

		app = mount(RobustnessPanel, {
			target,
			props: {
				strategyId: 'S0001',
				backtestHistory: [buildBacktestHistoryItem()],
				validationHistory: [],
				defaultSymbol: 'BTC/USDT',
				defaultTimeframe: '1h',
				symbolSuggestions: ['BTC/USDT'],
			},
		});

		await waitForCondition(() => apiMocks.getJob.mock.calls.length >= 2);
		await waitForCondition(() => backtestingMocks.getRobustnessResult.mock.calls.some((call) => call[0] === 'WF-REMOUNT'));
		await waitForCondition(() => (target.textContent ?? '').includes('PASS'));

		expect(target.textContent).toContain('0.910');
	});

	it('reports skipped and failed suite outcomes without a false success toast', async () => {
		backtestingMocks.submitWalkForwardRobustness.mockRejectedValue(new Error('Walk-forward unavailable'));
		backtestingMocks.submitCostStressRobustness.mockRejectedValue(new Error('Cost stress unavailable'));

		app = mount(RobustnessPanel, {
			target,
			props: {
				strategyId: 'S0001',
				backtestHistory: [],
				validationHistory: [],
				defaultSymbol: 'BTC/USDT',
				defaultTimeframe: '1h',
				symbolSuggestions: ['BTC/USDT'],
			},
		});

		clickButtonByText(target, 'Run Full Suite');

		await waitForCondition(() => trackerMocks.addToast.mock.calls.length > 0);

		const [message, type] = trackerMocks.addToast.mock.calls.at(-1) ?? [];
		expect(String(message)).toContain('Robustness suite: 0 queued, 0 running, 2 failed, 3 skipped.');
		expect(type).toBe('error');
	});
});
