import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { mount, unmount } from 'svelte';

import { get } from 'svelte/store';
import { clearDirty, dirtyFields, markField, originalValues, pendingValues } from '../lib/settings/dirty';

const apiMocks = vi.hoisted(() => ({
	getSettings: vi.fn(),
	getForvenDashboard: vi.fn(),
	getSettingsAuditLog: vi.fn(),
	updateSettingsSection: vi.fn(),
}));

vi.mock('$lib/api', () => apiMocks);

const nav = vi.hoisted(() => ({
	guard: null as null | ((navigation: { cancel: () => void; to: { url: URL } | null }) => void),
}));

vi.mock('$app/navigation', () => ({
	beforeNavigate: vi.fn((callback) => {
		nav.guard = callback;
	}),
	goto: vi.fn(),
}));

let target: HTMLElement;
let instance: any;

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

beforeEach(() => {
	clearDirty();
	originalValues.set({});
	pendingValues.set({});
	apiMocks.getSettings.mockResolvedValue({
		exchange: 'hyperliquid',
		trading_mode: 'paper',
		max_daily_loss: 150,
		risk: {},
		pipeline: {},
	});
	apiMocks.getForvenDashboard.mockResolvedValue({});
	apiMocks.getSettingsAuditLog.mockResolvedValue([]);
	apiMocks.updateSettingsSection.mockResolvedValue({ status: 'ok' });
	if (typeof window !== 'undefined') {
		window.location.hash = '';
	}
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target?.remove();
	vi.clearAllMocks();
});

describe('Settings page shell', () => {
	it('renders the sidebar with all seven area labels', async () => {
		const SettingsPage = (await import('../routes/settings/+page.svelte')).default;
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsPage, { target, props: {} });
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Home');
		expect(text).toContain('Lab');
		expect(text).toContain('Trading');
		expect(text).toContain('Data');
		expect(text).toContain('Notifications');
		expect(text).toContain('System');
		expect(text).toContain('Danger Zone');
	});

	it('defaults to the Home section when no hash is set', async () => {
		const SettingsPage = (await import('../routes/settings/+page.svelte')).default;
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsPage, { target, props: {} });
		await flush();

		// Home section renders the Daily Controls label or one of the tile labels.
		const text = target.textContent || '';
		expect(text).toMatch(/Daily Controls|Kill switch|Trading mode/i);
	});

	it('switches to the Trading section when hash is #trading', async () => {
		window.location.hash = '#trading';
		const SettingsPage = (await import('../routes/settings/+page.svelte')).default;
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsPage, { target, props: {} });
		await flush();

		const text = target.textContent || '';
		// Trading section renders manifest-driven subsection headings.
		expect(text).toContain('Exchange connection');
	});

	it('calls getSettings on mount', async () => {
		const SettingsPage = (await import('../routes/settings/+page.svelte')).default;
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsPage, { target, props: {} });
		await flush();

		expect(apiMocks.getSettings).toHaveBeenCalled();
	});

	it('"Discard & leave" really discards the pending edits', async () => {
		const SettingsPage = (await import('../routes/settings/+page.svelte')).default;
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsPage, { target, props: {} });
		await flush();

		markField('risk.max_daily_loss', 999);
		expect(get(dirtyFields).size).toBe(1);
		const cancel = vi.fn();
		nav.guard!({ cancel, to: { url: new URL('http://localhost/approval') } });
		await flush();
		expect(cancel).toHaveBeenCalled();

		const discard = [...target.querySelectorAll('button')].find((b) => b.textContent?.includes('Discard'));
		discard!.click();
		await flush();

		expect(get(dirtyFields).size).toBe(0);
		expect(get(pendingValues)).toEqual({});
	});
});
