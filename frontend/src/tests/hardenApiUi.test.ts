import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';
import { mount, unmount } from 'svelte';

const apiMock = vi.hoisted(() => ({
	getJob: vi.fn(),
	getJobs: vi.fn(async () => []),
	getScan: vi.fn(),
	listScans: vi.fn(async () => []),
	getTournament: vi.fn(),
	listTournaments: vi.fn(async () => []),
}));

vi.mock('$lib/api', () => apiMock);

import {
	addToast,
	clearSnooze,
	snoozeNotifications,
	toasts,
	trackProcess,
	trackedProcesses,
	untrackProcess,
} from '../lib/stores/processTracker';
import { notify } from '../lib/stores/toasts';
// NOT mocked on purpose — API-10 is a request-BODY contract, and a mocked
// performFactoryReset is exactly what let the 422 ship unnoticed.
import { FACTORY_RESET_CONFIRM_PHRASE, performFactoryReset } from '../lib/api/forven';
import SettingsDangerZone from '../lib/components/settings/sections/SettingsDangerZone.svelte';

beforeEach(() => {
	toasts.set([]);
	trackedProcesses.set([]);
	clearSnooze();
	apiMock.getJob.mockReset();
});

afterEach(() => {
	vi.useRealTimers();
	toasts.set([]);
	trackedProcesses.set([]);
	clearSnooze();
});

// --------------------------------------------------------------------------
// FE-02: snoozing must never swallow an error. Since the notification revamp a
// snooze pauses EVENT notifications only (notify(): fills, completions,
// alerts); the answer to something the operator just did — addToast(), errors
// above all — always shows.
// --------------------------------------------------------------------------

describe('FE-02 snooze never hides errors or action feedback', () => {
	it('keeps error and feedback toasts on screen when a snooze starts, clearing event chatter', () => {
		notify({ category: 'background_jobs', message: 'backtest completed', type: 'success' });
		addToast('GO LIVE promotion refused', 'error');

		snoozeNotifications(60_000);

		expect(get(toasts).map((t) => t.message)).toEqual(['GO LIVE promotion refused']);
	});

	it('still admits NEW errors and feedback while snoozed, but not routine events', () => {
		snoozeNotifications(60_000);

		expect(notify({ category: 'background_jobs', message: 'routine job finished' })).toBeNull();
		expect(addToast('order rejected by the venue', 'error')).not.toBeNull();
		expect(get(toasts).map((t) => t.message)).toEqual(['order rejected by the venue']);
	});
});

// --------------------------------------------------------------------------
// FE-09: the tracker's promised polling loop
// --------------------------------------------------------------------------

describe('FE-09 process tracker polls on an interval', () => {
	it('resolves a tracked job with no websocket event at all', async () => {
		vi.useFakeTimers();
		apiMock.getJob.mockResolvedValue({ id: 'job-1', status: 'running' });

		trackProcess('job-1', 'job', 'Job backtest', '/lab', {
			id: 'job-1',
			status: 'running',
		} as never);

		// createPoller fires one immediate tick on start().
		await vi.advanceTimersByTimeAsync(0);
		const callsAfterStart = apiMock.getJob.mock.calls.length;
		expect(callsAfterStart).toBeGreaterThan(0);

		// No `forven:event` is ever dispatched here — before the fix nothing else
		// would ever call getJob again and the job stayed 'running' forever.
		apiMock.getJob.mockResolvedValue({ id: 'job-1', status: 'succeeded' });
		await vi.advanceTimersByTimeAsync(9_000);

		expect(apiMock.getJob.mock.calls.length).toBeGreaterThan(callsAfterStart);
		expect(get(trackedProcesses)[0].status).toBe('succeeded');
		expect(get(toasts).map((t) => t.message)).toEqual(['Job backtest completed']);
	});

	it('stops polling once nothing is active', async () => {
		vi.useFakeTimers();
		apiMock.getJob.mockResolvedValue({ id: 'job-2', status: 'failed' });

		trackProcess('job-2', 'job', 'Job wfa', '/lab', { id: 'job-2', status: 'running' } as never);
		await vi.advanceTimersByTimeAsync(0);
		const callsWhileActive = apiMock.getJob.mock.calls.length;

		await vi.advanceTimersByTimeAsync(30_000);
		expect(apiMock.getJob.mock.calls.length).toBe(callsWhileActive);

		untrackProcess('job-2', 'job');
	});
});

// --------------------------------------------------------------------------
// API-10: the factory-reset request body, asserted for real
//
// The backend now types the confirmation (`FactoryResetBody.confirm_phrase` is a
// pydantic Literal). The client sent only `{keep}`, and the ONLY existing frontend
// test mocks `performFactoryReset` — so a 422 on the real Danger Zone button was
// invisible to both suites. These drive the real client against a fake `fetch` and
// assert the bytes that actually go on the wire.
// --------------------------------------------------------------------------

type FetchCall = [string, RequestInit];

function jsonResponse(payload: unknown, status = 200): Response {
	return {
		ok: status >= 200 && status < 300,
		status,
		statusText: '',
		json: async () => payload,
		text: async () => JSON.stringify(payload),
	} as unknown as Response;
}

function fetchCalls(): FetchCall[] {
	return (global.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls as FetchCall[];
}

function bodyOf(call: FetchCall): Record<string, unknown> {
	return JSON.parse(String(call[1].body)) as Record<string, unknown>;
}

describe('API-10 factory reset sends the typed confirmation', () => {
	beforeEach(() => {
		(global.fetch as unknown as ReturnType<typeof vi.fn>).mockReset();
	});

	it('posts confirm_phrase, keep and allow_credentials_wipe', async () => {
		const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
		fetchMock.mockResolvedValue(jsonResponse({ status: 'ok', wiped: [], kept: ['brain'] }));

		await performFactoryReset(['brain']);

		const call = fetchCalls().at(-1)!;
		expect(String(call[0])).toContain('/system/factory-reset');
		expect(call[1].method).toBe('POST');
		// The exact string the pydantic Literal accepts — nothing else is a 200.
		expect(bodyOf(call)).toEqual({
			confirm_phrase: 'FACTORY RESET',
			keep: ['brain'],
			allow_credentials_wipe: false,
		});
	});

	it('keeps [] distinguishable from an absent keep list', async () => {
		const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
		fetchMock.mockResolvedValue(jsonResponse({ status: 'ok', wiped: [], kept: [] }));

		await performFactoryReset([]);

		// `[]` means "wipe everything" server-side; it must survive serialisation
		// rather than being dropped or collapsed into the default keep set.
		expect(bodyOf(fetchCalls().at(-1)!).keep).toEqual([]);
	});

	it('only opts into a credentials wipe when explicitly asked', async () => {
		const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
		fetchMock.mockResolvedValue(jsonResponse({ status: 'ok', wiped: [], kept: [] }));

		await performFactoryReset([], { allowCredentialsWipe: true });
		expect(bodyOf(fetchCalls().at(-1)!).allow_credentials_wipe).toBe(true);
	});
});

describe('API-10 Danger Zone dialog', () => {
	let target: HTMLElement;
	let instance: unknown;

	async function flush(): Promise<void> {
		for (let i = 0; i < 4; i += 1) {
			await Promise.resolve();
			await new Promise((r) => setTimeout(r, 0));
		}
	}

	function routeFetch(): void {
		const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;
		fetchMock.mockReset();
		fetchMock.mockImplementation(async (url: string) => {
			if (String(url).includes('factory-reset/categories')) {
				return jsonResponse({
					categories: [
						{ id: 'brain', label: 'Brain memory', description: '', default_keep: true },
						{ id: 'market_data', label: 'Market data', description: '', default_keep: false },
					],
				});
			}
			return jsonResponse({ status: 'ok', wiped: ['market_data'], kept: ['brain'] });
		});
	}

	async function mountPanel(): Promise<void> {
		routeFetch();
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsDangerZone, { target, props: { settings: {} } });
		await flush();
		const trigger = Array.from(target.querySelectorAll('button')).find((b) =>
			/factory reset/i.test(b.textContent || ''),
		);
		trigger!.click();
		await flush();
	}

	function typeConfirmation(value: string): void {
		const input = target.querySelector('[role="dialog"] input') as HTMLInputElement;
		input.value = value;
		input.dispatchEvent(new Event('input', { bubbles: true }));
	}

	function wipeButton(): HTMLButtonElement {
		return Array.from(target.querySelectorAll('[role="dialog"] button')).find((b) =>
			/wipe everything/i.test(b.textContent || ''),
		) as HTMLButtonElement;
	}

	function resetCalls(): FetchCall[] {
		return fetchCalls().filter(
			(c) => String(c[0]).includes('/system/factory-reset') && c[1]?.method === 'POST',
		);
	}

	afterEach(() => {
		if (instance) unmount(instance as never);
		instance = undefined;
		target?.remove();
	});

	it('stays disarmed for the OLD phrase', async () => {
		await mountPanel();

		// 'RESET' was the phrase before API-10 — arming on it would post a body the
		// backend 422s, which is precisely the regression this test exists for.
		typeConfirmation('RESET');
		await flush();

		expect(wipeButton().disabled).toBe(true);
		wipeButton().click();
		await flush();
		expect(resetCalls()).toHaveLength(0);
	});

	it('arms on the full phrase and posts the body the backend accepts', async () => {
		await mountPanel();

		typeConfirmation(FACTORY_RESET_CONFIRM_PHRASE);
		await flush();
		expect(wipeButton().disabled).toBe(false);

		wipeButton().click();
		await flush();

		const posts = resetCalls();
		expect(posts).toHaveLength(1);
		expect(bodyOf(posts[0])).toEqual({
			confirm_phrase: 'FACTORY RESET',
			// 'brain' is default_keep, 'market_data' is not.
			keep: ['brain'],
			allow_credentials_wipe: false,
		});
		expect(target.textContent).toContain('Factory reset complete');
	});

	it('prompts the operator for the phrase the backend requires', async () => {
		await mountPanel();
		// The dialog's local constant must not drift from the client's, which must
		// not drift from the backend Literal. This is the drift guard for the first
		// link; the posted-body assertion above is the guard for the second.
		expect(target.querySelector('[role="dialog"]')!.textContent).toContain(
			FACTORY_RESET_CONFIRM_PHRASE,
		);
	});
});
