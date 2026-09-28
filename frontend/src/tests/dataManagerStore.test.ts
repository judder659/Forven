import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

const api = vi.hoisted(() => ({
	getJobsSummary: vi.fn(),
	getSlaCensus: vi.fn(),
}));
vi.mock('$lib/api/dataManager', () => api);

import { ApiError } from '../lib/api/core';
import {
	JOBS_FAST_MS,
	JOBS_SLOW_MS,
	JOBS_UNAVAILABLE_MS,
	createRequestGuard,
	jobsSummary,
	loadSlaCensus,
	nextJobsDelay,
	pokeJobs,
	resetDataManagerState,
	settle,
	slaCensus,
	startJobsPolling,
} from '../lib/stores/dataManager';
import { fixtureCensus } from '../lib/api/dataManagerFixtures';

const idle = { running: 0, queued: 0, failed_24h: 0, succeeded_24h: 3, last_routine: null };
const busy = { ...idle, running: 2 };

async function flush() {
	for (let i = 0; i < 5; i++) await Promise.resolve();
}

describe('settle', () => {
	it('maps success, a missing route and other errors', async () => {
		expect((await settle(Promise.resolve(1))).status).toBe('ready');
		const missing = await settle(Promise.reject(new ApiError(404, 'Not Found')));
		expect(missing.status).toBe('unavailable');
		const failed = await settle(Promise.reject(new ApiError(500, 'Internal Server Error')));
		expect(failed).toMatchObject({ status: 'error', error: 'The backend failed (HTTP 500).' });
	});

	it('keeps the previous data when a refresh fails', async () => {
		const previous = await settle(Promise.resolve({ n: 1 }));
		const next = await settle(Promise.reject(new Error('boom')), previous);
		expect(next).toMatchObject({ status: 'ready', data: { n: 1 }, error: 'boom' });
	});
});

describe('request guard', () => {
	it('only the newest request is current, and older ones are aborted', () => {
		const guard = createRequestGuard();
		const first = guard.next();
		const second = guard.next();
		expect(first.current()).toBe(false);
		expect(first.signal?.aborted).toBe(true);
		expect(second.current()).toBe(true);
		guard.cancel();
		expect(second.current()).toBe(false);
	});
});

describe('jobs polling', () => {
	beforeEach(() => {
		vi.useFakeTimers();
		resetDataManagerState();
		api.getJobsSummary.mockReset();
	});
	afterEach(() => {
		vi.useRealTimers();
	});

	it('picks the cadence from what is running', () => {
		expect(nextJobsDelay(busy, 'ready', 0, 1000)).toBe(JOBS_FAST_MS);
		expect(nextJobsDelay({ ...idle, queued: 1 }, 'ready', 0, 1000)).toBe(JOBS_FAST_MS);
		expect(nextJobsDelay(idle, 'ready', 0, 1000)).toBe(JOBS_SLOW_MS);
		expect(nextJobsDelay(idle, 'ready', 5000, 1000)).toBe(JOBS_FAST_MS);
		expect(nextJobsDelay(null, 'unavailable', 0, 1000)).toBe(JOBS_UNAVAILABLE_MS);
	});

	it('polls fast while jobs run, slows down after, and stops when released', async () => {
		api.getJobsSummary.mockResolvedValue(busy);
		const stop = startJobsPolling();
		await flush();
		expect(api.getJobsSummary).toHaveBeenCalledTimes(1);
		expect(get(jobsSummary).data).toEqual(busy);
		await vi.advanceTimersByTimeAsync(JOBS_FAST_MS);
		expect(api.getJobsSummary).toHaveBeenCalledTimes(2);
		api.getJobsSummary.mockResolvedValue(idle);
		await vi.advanceTimersByTimeAsync(JOBS_FAST_MS);
		expect(api.getJobsSummary).toHaveBeenCalledTimes(3);
		await vi.advanceTimersByTimeAsync(JOBS_FAST_MS * 2);
		expect(api.getJobsSummary).toHaveBeenCalledTimes(3);
		await vi.advanceTimersByTimeAsync(JOBS_SLOW_MS);
		expect(api.getJobsSummary).toHaveBeenCalledTimes(4);
		stop();
		await vi.advanceTimersByTimeAsync(JOBS_SLOW_MS * 3);
		expect(api.getJobsSummary).toHaveBeenCalledTimes(4);
	});

	it('pauses while the tab is hidden and resumes when it is shown', async () => {
		api.getJobsSummary.mockResolvedValue(busy);
		let hidden = true;
		Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
		try {
			const stop = startJobsPolling();
			await vi.advanceTimersByTimeAsync(JOBS_SLOW_MS * 2);
			expect(api.getJobsSummary).not.toHaveBeenCalled();
			hidden = false;
			document.dispatchEvent(new Event('visibilitychange'));
			await flush();
			expect(api.getJobsSummary).toHaveBeenCalledTimes(1);
			stop();
		} finally {
			delete (document as unknown as Record<string, unknown>).hidden;
		}
	});

	it('a poke refreshes at once and keeps polling fast', async () => {
		api.getJobsSummary.mockResolvedValue(idle);
		const stop = startJobsPolling();
		await flush();
		pokeJobs();
		await flush();
		expect(api.getJobsSummary).toHaveBeenCalledTimes(2);
		await vi.advanceTimersByTimeAsync(JOBS_FAST_MS);
		expect(api.getJobsSummary).toHaveBeenCalledTimes(3);
		stop();
	});

	it('backs off when the endpoint is not deployed', async () => {
		api.getJobsSummary.mockRejectedValue(new ApiError(404, 'Not Found'));
		const stop = startJobsPolling();
		await flush();
		expect(get(jobsSummary).status).toBe('unavailable');
		await vi.advanceTimersByTimeAsync(JOBS_SLOW_MS * 2);
		expect(api.getJobsSummary).toHaveBeenCalledTimes(1);
		await vi.advanceTimersByTimeAsync(JOBS_UNAVAILABLE_MS);
		expect(api.getJobsSummary).toHaveBeenCalledTimes(2);
		stop();
	});
});

describe('SLA census cache', () => {
	beforeEach(() => {
		resetDataManagerState();
		api.getSlaCensus.mockReset();
	});

	it('shares one request and serves the cache while young', async () => {
		const census = fixtureCensus();
		api.getSlaCensus.mockResolvedValue(census);
		const [a, b] = await Promise.all([loadSlaCensus(), loadSlaCensus()]);
		expect(a).toBe(census);
		expect(b).toBe(census);
		expect(api.getSlaCensus).toHaveBeenCalledTimes(1);
		expect(api.getSlaCensus).toHaveBeenCalledWith({ limit_worst: 100 });
		await loadSlaCensus({ maxAgeMs: 60_000 });
		expect(api.getSlaCensus).toHaveBeenCalledTimes(1);
		await loadSlaCensus({ force: true });
		expect(api.getSlaCensus).toHaveBeenCalledTimes(2);
		expect(get(slaCensus).status).toBe('ready');
	});

	it('remembers that the endpoint is missing for a while', async () => {
		api.getSlaCensus.mockRejectedValue(new ApiError(404, 'Not Found'));
		expect(await loadSlaCensus()).toBeNull();
		expect(await loadSlaCensus()).toBeNull();
		expect(api.getSlaCensus).toHaveBeenCalledTimes(1);
		expect(get(slaCensus).status).toBe('unavailable');
	});
});
