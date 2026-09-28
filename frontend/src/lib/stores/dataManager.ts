/**
 * Shared state for the Data Manager page (`/data-next`): the jobs summary that
 * drives the header indicator and the Jobs drawer (polled fast while jobs run,
 * slowly otherwise, paused while the tab is hidden), the SLA census cache the
 * header, Health view and /data nav badge share, the "work landed" signal views
 * reload on, and helpers every view uses to load data without stale responses
 * or white screens.
 */
import { get, readable, writable } from 'svelte/store';
import { ApiError, ApiOutcomeUnknownError, isRouteMissingError } from '$lib/api/core';
import { getJobsSummary, getSlaCensus } from '$lib/api/dataManager';
import type { DataJobSummary, SlaCensus } from '$lib/api/dataManagerTypes';

// ---------------------------------------------------------------- loading state

export type LoadStatus = 'loading' | 'ready' | 'unavailable' | 'error';

/** One view's data: `unavailable` = the route is not deployed yet (404 Not Found). */
export interface Loadable<T> {
	status: LoadStatus;
	data: T | null;
	error: string;
	at: number;
}

export function loading<T>(): Loadable<T> {
	return { status: 'loading', data: null, error: '', at: 0 };
}

export function errorMessage(error: unknown): string {
	if (error instanceof ApiOutcomeUnknownError) return error.message;
	if (error instanceof ApiError) return error.status >= 500 && /internal server error/i.test(error.message) ? `The backend failed (HTTP ${error.status}).` : error.message;
	if (error instanceof Error && error.name === 'TimeoutError') return 'The request timed out.';
	if (error instanceof Error && error.name === 'TypeError') return 'The backend is not reachable.';
	return error instanceof Error ? error.message : 'Something went wrong.';
}

/** Settle a request into a Loadable. A failed refresh keeps the data it had
 * (with the error noted) instead of blanking the view. */
export async function settle<T>(promise: Promise<T>, previous?: Loadable<T> | null): Promise<Loadable<T>> {
	try {
		const data = await promise;
		return { status: 'ready', data, error: '', at: Date.now() };
	} catch (error) {
		if (isRouteMissingError(error)) return { status: 'unavailable', data: null, error: '', at: Date.now() };
		if (previous?.data != null) return { ...previous, error: errorMessage(error) };
		return { status: 'error', data: null, error: errorMessage(error), at: Date.now() };
	}
}

/** Only the newest request may land: `next()` aborts the previous one and
 * returns `current()`, false once a newer request has started. */
export function createRequestGuard() {
	let seq = 0;
	let controller: AbortController | null = null;
	return {
		next(): { signal: AbortSignal | undefined; current: () => boolean } {
			controller?.abort();
			controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
			const mine = ++seq;
			return { signal: controller?.signal, current: () => mine === seq };
		},
		cancel(): void {
			controller?.abort();
			controller = null;
			seq += 1;
		},
	};
}

// ---------------------------------------------------------------- jobs summary

export const JOBS_FAST_MS = 3_000;
export const JOBS_SLOW_MS = 30_000;
export const JOBS_UNAVAILABLE_MS = 120_000;

export const jobsSummary = writable<Loadable<DataJobSummary>>(loading());

/** Bumps when the summary shows work finishing. Views that show lake state
 * reload on it, so a refresh, download or delete shows up without a manual
 * reload. */
export const jobsLanded = writable(0);

/** Did work finish between two summaries? Fewer jobs running or queued, more
 * finished in the last day, or a new automatic run that changed something (an
 * "everything current" collector tick changes nothing). */
export function workLanded(before: DataJobSummary | null, after: DataJobSummary | null): boolean {
	if (!before || !after) return false;
	if (after.running + after.queued < before.running + before.queued) return true;
	if (after.succeeded_24h + after.failed_24h > before.succeeded_24h + before.failed_24h) return true;
	const routine = after.last_routine;
	if (!routine || routine.id === before.last_routine?.id) return false;
	const result = (routine.result ?? {}) as { refreshed?: number; bars_added?: number };
	return routine.status !== 'succeeded' || Number(result.refreshed ?? 1) > 0 || Number(result.bars_added ?? 0) > 0;
}

/** Poll fast while anything runs or is queued (or right after the user started
 * work), slowly otherwise, and rarely while the endpoint does not exist. */
export function nextJobsDelay(summary: DataJobSummary | null, status: LoadStatus, boostUntil: number, now: number): number {
	if (status === 'unavailable') return JOBS_UNAVAILABLE_MS;
	if (now < boostUntil) return JOBS_FAST_MS;
	if (summary && summary.running + summary.queued > 0) return JOBS_FAST_MS;
	return JOBS_SLOW_MS;
}

let pollUsers = 0;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let pollInFlight = false;
let boostUntil = 0;

function onVisibility(): void {
	if (typeof document !== 'undefined' && !document.hidden && pollUsers > 0) void pollJobs();
}

function schedulePoll(): void {
	if (pollTimer) clearTimeout(pollTimer);
	pollTimer = null;
	if (!pollUsers) return;
	const current = get(jobsSummary);
	pollTimer = setTimeout(() => void pollJobs(), nextJobsDelay(current.data, current.status, boostUntil, Date.now()));
}

async function pollJobs(): Promise<void> {
	if (!pollUsers || pollInFlight) return;
	if (pollTimer) clearTimeout(pollTimer);
	pollTimer = null;
	// Hidden tab: no timer at all; the visibility listener resumes polling.
	if (typeof document !== 'undefined' && document.hidden) return;
	pollInFlight = true;
	try {
		const before = get(jobsSummary);
		const next = await settle(getJobsSummary(), before);
		jobsSummary.set(next);
		if (next.status === 'ready' && workLanded(before.data, next.data)) jobsLanded.update((n) => n + 1);
	} finally {
		pollInFlight = false;
		schedulePoll();
	}
}

/** Start polling the jobs summary; returns the matching stop. Ref-counted, so
 * the layout and a page can both hold it. */
export function startJobsPolling(): () => void {
	pollUsers += 1;
	if (pollUsers === 1) {
		if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility);
		void pollJobs();
	}
	let stopped = false;
	return () => {
		if (stopped) return;
		stopped = true;
		pollUsers = Math.max(0, pollUsers - 1);
		if (!pollUsers) {
			if (pollTimer) clearTimeout(pollTimer);
			pollTimer = null;
			if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
		}
	};
}

/** After starting, cancelling or retrying work: refresh now and poll fast for a while. */
export function pokeJobs(boostMs = 60_000): void {
	boostUntil = Date.now() + boostMs;
	if (pollUsers) void pollJobs();
}

// ---------------------------------------------------------------- SLA census

export const CENSUS_LIMIT_WORST = 100;
const CENSUS_RETRY_UNAVAILABLE_MS = 5 * 60_000;

export const slaCensus = writable<Loadable<SlaCensus>>(loading());
let censusRequest: Promise<SlaCensus | null> | null = null;

/** The census, from the cache when it is younger than `maxAgeMs`. Concurrent
 * callers share one request; a missing endpoint is re-checked every 5 min. */
export async function loadSlaCensus(options: { maxAgeMs?: number; force?: boolean } = {}): Promise<SlaCensus | null> {
	const current = get(slaCensus);
	const age = Date.now() - current.at;
	if (!options.force) {
		if (current.status === 'ready' && current.data && age < (options.maxAgeMs ?? 15_000)) return current.data;
		if (current.status === 'unavailable' && age < CENSUS_RETRY_UNAVAILABLE_MS) return null;
	}
	if (censusRequest) return censusRequest;
	censusRequest = (async () => {
		const next = await settle(getSlaCensus({ limit_worst: CENSUS_LIMIT_WORST }), get(slaCensus));
		slaCensus.set(next);
		return next.data;
	})();
	try {
		return await censusRequest;
	} finally {
		censusRequest = null;
	}
}

/** Test helper: forget cached state. */
export function resetDataManagerState(): void {
	slaCensus.set(loading());
	jobsSummary.set(loading());
	jobsLanded.set(0);
	censusRequest = null;
	boostUntil = 0;
}

/** Search box registered by the current page, so "/" focuses it instead of the header search. */
export const pageSearch = writable<HTMLInputElement | null>(null);

/** Wall clock for "12 s ago" captions, ticking every 5 s while anything shows one. */
export const clock = readable(Date.now(), (set) => {
	const id = setInterval(() => set(Date.now()), 5_000);
	return () => clearInterval(id);
});
