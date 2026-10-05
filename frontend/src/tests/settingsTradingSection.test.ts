import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import SettingsTrading from '../lib/components/settings/sections/SettingsTrading.svelte';
import { originalValues, clearDirty } from '../lib/settings/dirty';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
});

beforeEach(() => {
	clearDirty();
	originalValues.set({});
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

describe('SettingsTrading section', () => {
	it('renders the expected trading subsections from the manifest', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsTrading, {
			target,
			props: {
				settings: {
					max_daily_loss: 150,
					strict_regime_gating: true,
					exchange: 'hyperliquid',
					initial_capital: 10000,
				},
			},
		});
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Exchange connection');
		expect(text).toContain('Regime gating');
		expect(text).toContain('Loss limits');
	});

	it('seeds originalValues on mount from the flat settings blob', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsTrading, {
			target,
			props: {
				settings: {
					max_daily_loss_pct: 3,
				},
			},
		});
		await flush();

		const originals = get(originalValues);
		expect(originals['risk.max_daily_loss_pct']).toBe(3);
	});

	it('risk fields are bound to the keys enforcement reads (no placebo twins)', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsTrading, {
			target,
			props: {
				settings: {
					max_risk_per_trade_pct: 7,
					max_daily_loss_pct: 3,
					max_drawdown_pct: 30,
					// Legacy twins present in the blob must NOT be what the UI binds to.
					max_position_size_pct: 99,
					max_daily_loss: 9999,
				},
			},
		});
		await flush();

		const originals = get(originalValues);
		expect(originals['risk.max_risk_per_trade_pct']).toBe(7);
		expect(originals['risk.max_daily_loss_pct']).toBe(3);
		expect(originals['risk.max_drawdown_pct']).toBe(30);
		expect(originals['risk.max_position_size_pct']).toBeUndefined();
		expect(originals['risk.max_daily_loss']).toBeUndefined();
	});

	it('shows the value in force when mainnet locks replace a saved value', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsTrading, {
			target,
			props: {
				settings: {
					max_concurrent_positions: 25,
					risk_effective: {
						on_mainnet: true,
						leverage_cap: 3,
						locked: { max_concurrent_positions: { effective: 10, reason: 'Capped at 10 on mainnet' } },
					},
				},
			},
		});
		await flush();

		expect(target.querySelector('[data-testid="mainnet-risk-banner"]')?.textContent).toContain('1 saved value is replaced');
		const note = target.querySelector('[data-testid="in-force-risk.max_concurrent_positions"]');
		expect(note?.textContent).toContain('10');
		expect(note?.textContent).toContain('Capped at 10 on mainnet');
		expect(target.querySelector('[data-testid="in-force-risk.max_daily_loss_pct"]')).toBeNull();
	});
});
