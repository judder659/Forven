import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount, flushSync } from 'svelte';
import { get } from 'svelte/store';

const { getUpdateStatusMock, applyUpdateMock, setUpdateChannelMock, checkHealthMock } = vi.hoisted(() => ({
	getUpdateStatusMock: vi.fn(),
	applyUpdateMock: vi.fn(),
	setUpdateChannelMock: vi.fn(),
	checkHealthMock: vi.fn(),
}));

vi.mock('$lib/api/updates', () => ({
	getUpdateStatus: getUpdateStatusMock,
	applyUpdate: applyUpdateMock,
	setUpdateChannel: setUpdateChannelMock,
}));

vi.mock('$lib/api', () => ({
	checkHealth: checkHealthMock,
}));

import {
	updateStatus,
	updateProgress,
	updatePanelOpen,
	runUpdate,
	snoozeUpdate,
	readJustUpdated,
	clearJustUpdated,
} from '../lib/stores/updateStatus';
import type { UpdateStatus } from '../lib/api/updates';
import UpdatePill from '../lib/components/updates/UpdatePill.svelte';
import WhatsNewPanel from '../lib/components/updates/WhatsNewPanel.svelte';

const RELEASE = {
	version: '0.2.0',
	tag: 'v0.2.0',
	sha: 'abc123def456',
	sha_short: 'abc123def',
	date: '2026-11-02T07:00:00+00:00',
	title: 'Forven 0.2.0',
	urgent: false,
	sections: [
		{ title: 'New', items: ['Weekly releases with notes'] },
		{ title: 'Fixes', items: ['The update banner no longer appears for every change'] },
	],
};

function stableStatus(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
	return {
		supported: true,
		channel: 'stable',
		channel_locked: false,
		current_version: '0.1.34',
		update_available: true,
		can_apply: true,
		urgent: false,
		behind: 12,
		target_sha: RELEASE.sha,
		target_version: '0.2.0',
		pending_releases: [RELEASE],
		latest_release: RELEASE,
		releases: [RELEASE],
		...overrides,
	};
}

let target: HTMLElement;
let instances: any[] = [];

function render(component: any) {
	const instance = mount(component, { target });
	instances.push(instance);
	flushSync();
	return instance;
}

async function flush(): Promise<void> {
	for (let i = 0; i < 4; i += 1) await Promise.resolve();
	flushSync();
}

beforeEach(() => {
	target = document.createElement('div');
	document.body.appendChild(target);
	localStorage.clear();
	getUpdateStatusMock.mockReset();
	applyUpdateMock.mockReset();
	checkHealthMock.mockReset();
	updateStatus.set(null);
	updateProgress.set(null);
	updatePanelOpen.set(false);
});

afterEach(() => {
	for (const instance of instances) unmount(instance);
	instances = [];
	target.remove();
	vi.useRealTimers();
});

describe('runUpdate', () => {
	it('walks Download, Install, Restart, Back online and reloads', async () => {
		vi.useFakeTimers();
		getUpdateStatusMock.mockResolvedValue(stableStatus());
		applyUpdateMock.mockResolvedValue({
			status: 'updated',
			restart_pending: true,
			from_version: '0.1.34',
			to_version: '0.2.0',
		});
		checkHealthMock.mockResolvedValue({ status: 'ok' });
		const reload = vi.fn();

		const run = runUpdate(reload);
		await vi.runAllTimersAsync();
		await run;
		await vi.runAllTimersAsync();

		expect(getUpdateStatusMock).toHaveBeenCalledWith(true);
		expect(applyUpdateMock).toHaveBeenCalledWith(false);
		expect(get(updateProgress)).toEqual({ step: 3, failed: false, error: null, done: true });
		expect(reload).toHaveBeenCalledTimes(1);
		const record = readJustUpdated();
		expect(record?.to_version).toBe('0.2.0');
		expect(record?.highlights).toEqual([
			'Weekly releases with notes',
			'The update banner no longer appears for every change',
		]);
		clearJustUpdated();
	});

	it('stops on Install with the reason when the update is refused', async () => {
		getUpdateStatusMock.mockResolvedValue(stableStatus());
		applyUpdateMock.mockResolvedValue({
			status: 'failed',
			restart_pending: false,
			reason: 'error: untracked file would be overwritten',
		});

		await runUpdate(vi.fn());

		expect(get(updateProgress)).toEqual({
			step: 1,
			failed: true,
			error: 'error: untracked file would be overwritten',
			done: false,
		});
	});

	it('stops on Download when local changes block the update', async () => {
		getUpdateStatusMock.mockResolvedValue(
			stableStatus({ can_apply: false, blocked_reason: 'This copy has uncommitted changes.' }),
		);

		await runUpdate(vi.fn());

		expect(applyUpdateMock).not.toHaveBeenCalled();
		expect(get(updateProgress)?.step).toBe(0);
		expect(get(updateProgress)?.error).toBe('This copy has uncommitted changes.');
	});
});

describe('UpdatePill', () => {
	it('names the release and opens the panel', () => {
		updateStatus.set(stableStatus());
		render(UpdatePill);

		const pill = target.querySelector('[data-testid="update-pill"]') as HTMLButtonElement;
		expect(pill.textContent).toContain('v0.2.0 available');
		pill.click();
		expect(get(updatePanelOpen)).toBe(true);
	});

	it('hides after Remind me tomorrow, but never for an urgent release', () => {
		updateStatus.set(stableStatus());
		render(UpdatePill);
		snoozeUpdate();
		flushSync();
		expect(target.querySelector('[data-testid="update-pill"]')).toBeNull();

		updateStatus.set(stableStatus({ urgent: true }));
		flushSync();
		const pill = target.querySelector('[data-testid="update-pill"]');
		expect(pill?.textContent).toContain('Urgent');
	});

	it('counts changes on the Latest channel', () => {
		updateStatus.set(
			stableStatus({ channel: 'latest', target_version: null, behind: 3, pending_releases: [], pending_commits: ['a', 'b', 'c'] }),
		);
		render(UpdatePill);
		expect(target.textContent).toContain('3 new changes');
	});
});

describe('WhatsNewPanel', () => {
	it('shows the release notes grouped by section with Update now', async () => {
		updateStatus.set(stableStatus());
		updatePanelOpen.set(true);
		render(WhatsNewPanel);
		await flush();

		const panel = document.querySelector('[data-testid="whats-new-panel"]') as HTMLElement;
		expect(panel.textContent).toContain('Forven 0.2.0');
		expect(panel.textContent).toContain("You're on 0.1.34");
		expect(panel.textContent).toContain('New');
		expect(panel.textContent).toContain('Weekly releases with notes');
		expect(panel.textContent).toContain('Fixes');
		const buttons = Array.from(panel.querySelectorAll('button')).map((b) => b.textContent?.trim());
		expect(buttons).toContain('Update now');
		expect(buttons).toContain('Remind me tomorrow');
	});

	it('shows the failed step and a Retry button', async () => {
		updateStatus.set(stableStatus());
		updateProgress.set({ step: 2, failed: true, error: "Forven didn't come back after restarting.", done: false });
		updatePanelOpen.set(true);
		render(WhatsNewPanel);
		await flush();

		const panel = document.querySelector('[data-testid="whats-new-panel"]') as HTMLElement;
		expect(panel.textContent).toContain("Forven didn't come back after restarting.");
		expect(panel.querySelector('[aria-label="Update progress"]')?.textContent).toContain('(failed)');
		const buttons = Array.from(panel.querySelectorAll('button')).map((b) => b.textContent?.trim());
		expect(buttons).toContain('Retry');
	});
});
