import { describe, it, expect, afterEach, beforeEach } from 'vitest';
import { mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import SettingsSystem from '../lib/components/settings/sections/SettingsSystem.svelte';
import { originalValues, pendingValues, markField, clearDirty } from '../lib/settings/dirty';

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

describe('SettingsSystem section', () => {
	it('renders the expected system subsections from the manifest', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSystem, {
			target,
			props: {
				settings: {
					self_healing_enabled: true,
					auto_restart_on_crash: true,
				},
			},
		});
		await flush();

		const text = target.textContent || '';
		expect(text).toContain('Remote engine');
		expect(text).toContain('Health & telemetry');
	});

	it('seeds originalValues on mount from the flat settings blob', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSystem, {
			target,
			props: {
				settings: {
					remote_engine_enabled: true,
				},
			},
		});
		await flush();

		const originals = get(originalValues);
		expect(originals['bot-operations.remote_engine_enabled']).toBe(true);
	});
});

// Backend-shaped preset bundles (values arbitrary but distinct per preset —
// derivation only compares, it never interprets).
const THROUGHPUT_BUNDLES = {
	trickle: {
		ideation_interval_minutes: 480,
		coding_interval_minutes: 240,
		testing_interval_minutes: 240,
		graduation_interval_minutes: 480,
		agent_task_claim_limit: 1,
		backtest_subprocess_budget: 1,
		gauntlet_drain_workers: 1,
		crucible_daily_develop_budget: 20,
	},
	conserve: {
		ideation_interval_minutes: 240,
		coding_interval_minutes: 120,
		testing_interval_minutes: 120,
		graduation_interval_minutes: 240,
		agent_task_claim_limit: 3,
		backtest_subprocess_budget: 2,
		gauntlet_drain_workers: 1,
		crucible_daily_develop_budget: 60,
	},
	balanced: {
		ideation_interval_minutes: 120,
		coding_interval_minutes: 60,
		testing_interval_minutes: 60,
		graduation_interval_minutes: 120,
		agent_task_claim_limit: 12,
		backtest_subprocess_budget: 4,
		gauntlet_drain_workers: 3,
		crucible_daily_develop_budget: 150,
	},
	max: {
		ideation_interval_minutes: 15,
		coding_interval_minutes: 15,
		testing_interval_minutes: 5,
		graduation_interval_minutes: 60,
		agent_task_claim_limit: 20,
		backtest_subprocess_budget: 8,
		gauntlet_drain_workers: 6,
		crucible_daily_develop_budget: 500,
	},
};

function settingsForBundle(bundle: Record<string, number>): Record<string, unknown> {
	const { crucible_daily_develop_budget, ...flat } = bundle;
	return {
		...flat,
		research_settings: { hypothesis_discipline: { crucible_daily_develop_budget } },
		throughput_presets: THROUGHPUT_BUNDLES,
	};
}

function dialSelect(): HTMLSelectElement {
	const select = target.querySelector('#throughput-preset-select') as HTMLSelectElement;
	expect(select).toBeTruthy();
	return select;
}

describe('SettingsSystem throughput preset dial', () => {
	it('derives the active preset from current values (no stored name)', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSystem, {
			target,
			props: { settings: settingsForBundle(THROUGHPUT_BUNDLES.conserve) },
		});
		await flush();

		expect(dialSelect().value).toBe('conserve');
	});

	it('derives custom when values match no bundle', async () => {
		const settings = settingsForBundle(THROUGHPUT_BUNDLES.conserve);
		settings.ideation_interval_minutes = 200;
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSystem, { target, props: { settings } });
		await flush();

		expect(dialSelect().value).toBe('custom');
	});

	it('picking a preset fills every bundle knob as pending edits', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSystem, {
			target,
			props: { settings: settingsForBundle(THROUGHPUT_BUNDLES.balanced) },
		});
		await flush();

		const select = dialSelect();
		select.value = 'trickle';
		select.dispatchEvent(new Event('change'));
		await flush();

		const pend = get(pendingValues);
		expect(pend['bot-operations.ideation_interval_minutes']).toBe(480);
		expect(pend['bot-operations.agent_task_claim_limit']).toBe(1);
		expect(pend['research.crucible_daily_develop_budget']).toBe(20);
		// Derived value follows the pending edits before any save.
		expect(dialSelect().value).toBe('trickle');
		// The RENDERED inputs must update too — regression guard: rows fed from a
		// helper that read $pendingValues internally never re-rendered on
		// programmatic fills (store had the values, UI showed stale ones).
		const ideationInput = target.querySelector(
			'input[id="bot-operations.ideation_interval_minutes"]',
		) as HTMLInputElement;
		expect(ideationInput).toBeTruthy();
		expect(ideationInput.value).toBe('480');
		const budgetInput = target.querySelector(
			'input[id="research.crucible_daily_develop_budget"]',
		) as HTMLInputElement;
		expect(budgetInput).toBeTruthy();
		expect(budgetInput.value).toBe('20');
	});

	it('editing one knob away flips the derived value to custom', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSystem, {
			target,
			props: { settings: settingsForBundle(THROUGHPUT_BUNDLES.conserve) },
		});
		await flush();
		expect(dialSelect().value).toBe('conserve');

		markField('bot-operations.ideation_interval_minutes', 200);
		await flush();

		expect(dialSelect().value).toBe('custom');
	});
});
