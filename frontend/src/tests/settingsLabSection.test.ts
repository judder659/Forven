import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import SettingsAreaView from '../lib/components/settings/SettingsAreaView.svelte';
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

describe('Settings Backtesting area', () => {
	it('renders the expected lab subsections from the manifest', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsAreaView, {
			target,
			props: {
				area: 'backtesting',
				settings: {
					research_settings: {},
					backtest_duration_days: 45,
					rolling_backtest_days: 30,
				},
			},
		});
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Research');
		expect(text).toContain('Defaults');
		expect(text).toContain('Window per pipeline stage');
	});

	it('seeds originalValues on mount from the flat settings blob', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsAreaView, {
			target,
			props: {
				area: 'backtesting',
				settings: {
					backtest_duration_days: 45,
				},
			},
		});
		await flush();

		const originals = get(originalValues);
		expect(originals['backtesting-defaults.backtest_duration_days']).toBe(45);
	});
});
