import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import { get } from 'svelte/store';

const apiMocks = vi.hoisted(() => ({
	getNotificationPreferences: vi.fn(),
	updateNotificationPreferences: vi.fn(),
	sendNotificationTest: vi.fn(),
}));

vi.mock('$lib/api', () => apiMocks);

import SettingsNotifications from '../lib/components/settings/sections/SettingsNotifications.svelte';
import { originalValues, clearDirty, dirtyFields } from '../lib/settings/dirty';
import { notificationPrefs } from '../lib/stores/notificationPrefs';
import { defaultNotificationPrefs } from '../lib/notifications/catalog';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
});

beforeEach(() => {
	clearDirty();
	originalValues.set({});
	notificationPrefs.set(defaultNotificationPrefs());
	apiMocks.getNotificationPreferences.mockReset().mockResolvedValue({ ...defaultNotificationPrefs(), discord_mode: 'policy', response_channels: ['chat'] });
	apiMocks.updateNotificationPreferences.mockReset().mockImplementation(async (updates: Record<string, unknown>) => ({
		...defaultNotificationPrefs(),
		...updates,
	}));
	apiMocks.sendNotificationTest.mockReset().mockResolvedValue({ ok: true, item: {} });
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

function render(settings: Record<string, unknown>) {
	target = document.createElement('div');
	document.body.appendChild(target);
	instance = mount(SettingsNotifications, { target, props: { settings } });
}

function rowSwitches(categoryId: string): HTMLButtonElement[] {
	return Array.from(target.querySelectorAll(`tr[data-category="${categoryId}"] button[role="switch"]`));
}

describe('SettingsNotifications section', () => {
	it('puts every notification setting in one place: events, badges, Discord', async () => {
		render({ discord_bot_token_configured: true, notification_level: 'all' });
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Where notifications go');
		expect(text).toContain('Sidebar badges');
		expect(text).toContain('Discord transport');
		expect(text).toContain('Delivery policy');
		// The coarse Discord-only toggles were replaced by the per-event matrix.
		expect(text).not.toContain('Event subscriptions');
		expect(target.querySelector('tr[data-category="paper_trade_opened"]')).not.toBeNull();
		expect(target.querySelector('li[data-badge="/approval"]')).not.toBeNull();
	});

	it('shows paper trades off and live trades on by default', async () => {
		render({ discord_bot_token_configured: true });
		await flush();

		const [paperPopup] = rowSwitches('paper_trade_opened');
		const [livePopup] = rowSwitches('live_trade_opened');
		expect(paperPopup.getAttribute('aria-checked')).toBe('false');
		expect(livePopup.getAttribute('aria-checked')).toBe('true');
	});

	it('marks safety alerts as always on instead of offering a switch', async () => {
		render({ discord_bot_token_configured: true });
		await flush();

		const row = target.querySelector('tr[data-category="risk_halt"]');
		expect(row?.textContent).toContain('Always');
		expect(rowSwitches('risk_halt')).toHaveLength(0);
	});

	it('saves a switch as soon as it is flipped, without the settings save bar', async () => {
		render({ discord_bot_token_configured: true });
		await flush();

		const [paperPopup, paperDiscord] = rowSwitches('paper_trade_closed');
		paperPopup.click();
		await flush();
		expect(apiMocks.updateNotificationPreferences).toHaveBeenCalledWith({ popup_paper_trade_closed: true });
		expect(get(notificationPrefs).popup_paper_trade_closed).toBe(true);

		paperDiscord.click();
		await flush();
		expect(apiMocks.updateNotificationPreferences).toHaveBeenLastCalledWith({ paper_trade_closed_to_discord: false });
		expect(get(dirtyFields).size).toBe(0);
	});

	it('rolls a switch back when the save fails', async () => {
		apiMocks.updateNotificationPreferences.mockRejectedValueOnce(new Error('backend down'));
		render({ discord_bot_token_configured: true });
		await flush();

		const [livePopup] = rowSwitches('live_trade_opened');
		livePopup.click();
		await flush();

		expect(get(notificationPrefs).popup_live_trade_opened).toBe(true);
		expect(target.textContent).toContain('backend down');
	});

	it('says when Discord is not connected', async () => {
		render({ discord_bot_token_configured: false });
		await flush();
		expect(target.textContent).toContain("Discord isn't connected");
	});

	it('sends a test notification', async () => {
		render({ discord_bot_token_configured: true });
		await flush();

		const button = Array.from(target.querySelectorAll('button')).find((b) => b.textContent?.includes('Send a test'));
		button?.click();
		await flush();
		expect(apiMocks.sendNotificationTest).toHaveBeenCalledWith('notification_test');
	});

	it('seeds originalValues on mount from the flat settings blob', async () => {
		render({ discord_bot_token: 'token-x' });
		await flush();

		const originals = get(originalValues);
		expect(originals['notifications.discord_bot_token']).toBe('token-x');
	});
});
