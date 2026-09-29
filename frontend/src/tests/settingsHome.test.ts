import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';

const { getSettingsAuditLogMock, getForvenDashboardMock } = vi.hoisted(() => ({
	getSettingsAuditLogMock: vi.fn(),
	getForvenDashboardMock: vi.fn(),
}));

vi.mock('$lib/api', () => ({
	getSettingsAuditLog: getSettingsAuditLogMock,
	getForvenDashboard: getForvenDashboardMock,
}));

import SettingsHome from '../lib/components/settings/sections/SettingsHome.svelte';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
	if (typeof window !== 'undefined') {
		window.location.hash = '';
	}
});

beforeEach(() => {
	getSettingsAuditLogMock.mockReset();
	getForvenDashboardMock.mockReset();
	getSettingsAuditLogMock.mockResolvedValue([]);
	getForvenDashboardMock.mockResolvedValue({ execution_mode: 'paper' });
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

describe('SettingsHome', () => {
	it('renders the four daily-control tiles', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsHome, {
			target,
			props: {
				settings: { trading_mode: 'paper', self_healing_enabled: true },
				dashboard: { execution_mode: 'paper' },
			},
		});
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('System');
		expect(text).toContain('Mode');
		expect(text).toContain('Kill Switch');
		expect(text).toContain('Self-healing');
	});

	it('renders a recently-changed entry after getSettingsAuditLog resolves', async () => {
		getSettingsAuditLogMock.mockResolvedValue([
			{ id: 'risk.max_daily_loss', from: 200, to: 150, at: '2026-04-17T10:00:00Z', actor: 'ui' },
		]);

		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsHome, {
			target,
			props: {
				settings: { trading_mode: 'paper' },
				dashboard: { execution_mode: 'paper' },
			},
		});
		await flush();
		await flush();

		expect(getSettingsAuditLogMock).toHaveBeenCalled();
		const text = target.textContent || '';
		expect(text).toContain('risk.max_daily_loss');
	});

	it('formats object and null audit values via the str() helper', async () => {
		getSettingsAuditLogMock.mockResolvedValue([
			{
				id: 'system.some_object',
				from: null,
				to: { mode: 'live', verbose: true },
				at: '2026-04-17T11:00:00Z',
				actor: 'ui',
			},
			{
				id: 'system.cleared',
				from: 'old',
				to: null,
				at: '2026-04-17T12:00:00Z',
				actor: 'ui',
			},
		]);

		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsHome, {
			target,
			props: {
				settings: { trading_mode: 'paper' },
				dashboard: { execution_mode: 'paper' },
			},
		});
		await flush();
		await flush();

		const text = target.textContent || '';
		// Object `to` should be serialized to JSON-ish output — exercises the
		// `typeof v === 'object' && v !== null` → JSON.stringify branch.
		expect(text).toContain('"mode":"live"');
		// Null `to` should produce the em-dash fallback — exercises the
		// `v == null` branch.
		expect(text).toContain('—');
	});

	it('renders a needs-config callout when hyperliquid has no credentials', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsHome, {
			target,
			props: {
				settings: {
					exchange: 'hyperliquid',
					hyperliquid_wallet: '',
					hyperliquid_has_key: false,
					trading_mode: 'paper',
				},
				dashboard: { execution_mode: 'paper' },
			},
		});
		await flush();

		const text = target.textContent || '';
		expect(text.toLowerCase()).toContain('hyperliquid');
	});

	it('does not flag an unconnected Discord as a setup gap — notifications still reach the app', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsHome, {
			target,
			props: {
				settings: {
					notification_level: 'all',
					discord_bot_token_configured: false,
					discord_webhook_configured: false,
				},
				dashboard: { execution_mode: 'paper' },
			},
		});
		await flush();

		const text = (target.textContent || '').toLowerCase();
		expect(text).not.toContain('no discord transport');
	});

	it('exposes a working settings search input', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsHome, {
			target,
			props: {
				settings: {},
				dashboard: { execution_mode: 'paper' },
			},
		});
		await flush();

		const input = target.querySelector('input[type="search"]') as HTMLInputElement;
		expect(input).toBeTruthy();
		expect(input.getAttribute('role')).toBe('combobox');
	});
});
