import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import type { ReadinessReport, ReadinessRequirement } from '../lib/api/dataManagerTypes';

const api = vi.hoisted(() => ({
	checkReadiness: vi.fn(),
	getStrategyReadiness: vi.fn(),
}));

vi.mock('$lib/api/dataReadiness', async (importOriginal) => ({
	...(await importOriginal<typeof import('../lib/api/dataReadiness')>()),
	checkReadiness: api.checkReadiness,
	getStrategyReadiness: api.getStrategyReadiness,
}));

import ReadinessChip from '../lib/components/data-readiness/ReadinessChip.svelte';
import ReadinessHarness from './fixtures/ReadinessHarness.svelte';

function requirement(overrides: Partial<ReadinessRequirement>): ReadinessRequirement {
	return {
		key: 'series:ohlcv:canonical:BTC-USDT:1h',
		kind: 'series',
		label: 'BTC-USDT 1h candles',
		symbol: 'BTC-USDT',
		timeframe: '1h',
		stream: 'ohlcv',
		min_history_days: 739,
		status: 'ok',
		detail: '17,760 bars stored; the research window is 100.0% complete.',
		fix: null,
		...overrides,
	};
}

function report(verdict: ReadinessReport['verdict'], requirements: ReadinessRequirement[], summary: string): ReadinessReport {
	return { subject: { symbol: 'BTC-USDT', timeframe: '1h' }, verdict, summary, requirements, generated_at: '2026-09-28T12:00:00Z' };
}

const READY = report('ready', [requirement({})], 'Ready: BTC-USDT 1h candles are stored, complete and current.');
const NEEDS = report(
	'needs_data',
	[
		requirement({}),
		requirement({
			key: 'stream:oi:BTC-USDT',
			kind: 'stream',
			label: 'Open interest',
			stream: 'oi',
			status: 'missing',
			detail: 'The strategy reads open_interest, but no open interest for BTC-USDT is stored.',
			fix: {
				action: 'download',
				label: 'Download open interest for BTC-USDT',
				request: { symbol: 'BTC-USDT', timeframe: '1h', venue: 'canonical', history: { mode: 'days', days: 730 }, streams: ['oi'] },
			},
		}),
	],
	'Needs data: download open interest for BTC-USDT.',
);
const BLOCKED = report(
	'blocked',
	[
		requirement({
			key: 'stream:liquidations:BTC-USDT',
			kind: 'stream',
			label: 'Liquidations',
			stream: 'liquidations',
			status: 'blocked',
			detail: 'Liquidations for BTC-USDT starts 2026-07-06, after the research window starts (2024-09-28).',
		}),
	],
	'Blocked: Liquidations for BTC-USDT starts 2026-07-06, after the research window starts (2024-09-28).',
);

async function settle(): Promise<void> {
	for (let i = 0; i < 5; i += 1) {
		await Promise.resolve();
		await tick();
	}
}

describe('ReadinessChip', () => {
	let target: HTMLDivElement;
	let app: ReturnType<typeof mount> | null = null;

	beforeEach(() => {
		vi.useFakeTimers();
		target = document.createElement('div');
		document.body.appendChild(target);
		api.checkReadiness.mockReset();
		api.getStrategyReadiness.mockReset();
	});

	afterEach(() => {
		if (app) unmount(app);
		app = null;
		target.remove();
		vi.useRealTimers();
	});

	async function mountWith(props: Record<string, unknown>) {
		app = mount(ReadinessChip, { target, props });
		flushSync();
		await vi.advanceTimersByTimeAsync(600);
		await settle();
	}

	const chip = () => target.querySelector<HTMLButtonElement>('[data-testid="readiness-chip"]');

	it('shows a quiet placeholder while it checks, then the verdict', async () => {
		let resolve: (value: ReadinessReport) => void = () => {};
		api.checkReadiness.mockReturnValue(new Promise<ReadinessReport>((r) => (resolve = r)));
		app = mount(ReadinessChip, { target, props: { query: { symbol: 'BTC/USDT', timeframe: '1h' } } });
		flushSync();
		expect(target.querySelector('[data-testid="readiness-loading"]')?.textContent).toContain('Checking data');
		await vi.advanceTimersByTimeAsync(600);
		resolve(READY);
		await settle();
		expect(target.querySelector('[data-testid="readiness-loading"]')).toBeNull();
		expect(chip()?.dataset.verdict).toBe('ready');
		expect(chip()?.textContent).toContain('Data ready');
		expect(target.querySelector('[data-testid="readiness-panel"]')).toBeNull();

		chip()?.click();
		await settle();
		const panel = target.querySelector('[data-testid="readiness-panel"]');
		expect(panel?.textContent).toContain('BTC-USDT · 1h');
		expect(target.querySelector('[data-testid="readiness-summary"]')?.textContent).toBe(READY.summary);
		expect(target.querySelectorAll('[data-testid="readiness-requirement"]')).toHaveLength(1);
		expect(panel?.querySelector('a')).toBeNull(); // nothing to fix
	});

	it('lists what is missing with a fix link to Get data', async () => {
		api.checkReadiness.mockResolvedValue(NEEDS);
		await mountWith({ query: { symbol: 'BTC/USDT', timeframe: '1h', streams: ['oi'] } });
		expect(chip()?.dataset.verdict).toBe('needs_data');
		expect(chip()?.textContent).toContain('Needs data');
		expect(chip()?.textContent).toContain('1 fix');
		chip()?.click();
		await settle();
		const missing = target.querySelector('[data-testid="readiness-requirement"][data-status="missing"]');
		expect(missing?.textContent).toContain('Open interest');
		expect(missing?.textContent).toContain('Missing');
		const link = missing?.querySelector('a');
		expect(link?.getAttribute('href')).toBe('/data-next/get?symbol=BTC-USDT&timeframe=1h&venue=canonical&history=730d&streams=oi');
		expect(link?.textContent).toContain('Download open interest for BTC-USDT');
	});

	it('says why the data is blocked', async () => {
		api.getStrategyReadiness.mockResolvedValue(BLOCKED);
		await mountWith({ strategyId: 'S05577' });
		expect(api.getStrategyReadiness).toHaveBeenCalledWith('S05577');
		expect(api.checkReadiness).not.toHaveBeenCalled();
		expect(chip()?.dataset.verdict).toBe('blocked');
		expect(chip()?.textContent).toContain('Data blocked');
		expect(chip()?.getAttribute('title')).toBe(BLOCKED.summary);
		chip()?.click();
		await settle();
		const row = target.querySelector('[data-testid="readiness-requirement"][data-status="blocked"]');
		expect(row?.textContent).toContain('2026-07-06');
		expect(row?.querySelector('a')).toBeNull();
	});

	it('renders nothing when the check fails or has nothing to check', async () => {
		api.checkReadiness.mockRejectedValue(new Error('404 Not Found'));
		await mountWith({ query: { symbol: 'BTC/USDT', timeframe: '1h' } });
		expect(target.innerHTML.replace(/<!---->/g, '').trim()).toBe('');

		if (app) unmount(app);
		await mountWith({ query: { symbol: '  ', timeframe: '1h' } });
		expect(target.querySelector('[data-testid="readiness"]')).toBeNull();
		expect(api.checkReadiness).toHaveBeenCalledTimes(1);
	});

	it('debounces edits into one request with the latest query, and ignores stale answers', async () => {
		const harness = mount(ReadinessHarness, { target, props: { query: { symbol: 'B', timeframe: '1h' } } }) as unknown as {
			setQuery: (q: unknown) => void;
		};
		app = harness as unknown as ReturnType<typeof mount>;
		api.checkReadiness.mockResolvedValue(READY);
		flushSync();
		await vi.advanceTimersByTimeAsync(200);
		harness.setQuery({ symbol: 'BT', timeframe: '1h' });
		flushSync();
		await vi.advanceTimersByTimeAsync(200);
		harness.setQuery({ symbol: 'BTC/USDT ', timeframe: '1h' });
		flushSync();
		await vi.advanceTimersByTimeAsync(600);
		await settle();
		expect(api.checkReadiness).toHaveBeenCalledTimes(1);
		expect(api.checkReadiness).toHaveBeenCalledWith({ symbol: 'BTC/USDT', timeframe: '1h' });
		expect(chip()?.dataset.verdict).toBe('ready');

		// A slow answer for an older query never replaces the newer one.
		let resolveOld: (value: ReadinessReport) => void = () => {};
		api.checkReadiness.mockReturnValueOnce(new Promise<ReadinessReport>((r) => (resolveOld = r)));
		harness.setQuery({ symbol: 'BTC/USDT', timeframe: '4h' });
		flushSync();
		await vi.advanceTimersByTimeAsync(600);
		expect(chip()?.className).toContain('opacity-60'); // the old verdict stays while it re-checks
		api.checkReadiness.mockResolvedValueOnce(BLOCKED);
		harness.setQuery({ symbol: 'BTC/USDT', timeframe: '1d' });
		flushSync();
		await vi.advanceTimersByTimeAsync(600);
		await settle();
		resolveOld(NEEDS);
		await settle();
		expect(chip()?.dataset.verdict).toBe('blocked');
		expect(chip()?.className).not.toContain('opacity-60');
	});
});
