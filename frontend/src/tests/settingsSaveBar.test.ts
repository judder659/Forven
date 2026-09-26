import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import { get } from 'svelte/store';

// Mock manifest with two valid entries so Save paths exercise a real grouping.
vi.mock('../lib/settings/manifest', () => ({
	SETTINGS_MANIFEST: [
		{
			id: 'risk.max_daily_loss',
			label: 'Max daily loss',
			default: 0,
			type: 'number',
			area: 'trading',
			subsection: 'risk',
			backendSection: 'risk',
			backendPath: 'max_daily_loss',
			description: '',
			usedBy: [],
		},
		{
			id: 'risk.max_drawdown_pct',
			label: 'Max drawdown %',
			default: 0,
			type: 'number',
			area: 'trading',
			subsection: 'risk',
			backendSection: 'risk',
			backendPath: 'max_drawdown_pct',
			description: '',
			usedBy: [],
		},
		{
			// Second backend section so multi-section saves have two PUTs to order.
			id: 'research.strategy_creation_daily_budget',
			label: 'Daily strategy-creation budget',
			default: 12,
			type: 'number',
			area: 'system',
			subsection: 'system-throughput',
			backendSection: 'research',
			backendPath: 'research_settings.strategy_creation_daily_budget',
			description: '',
			usedBy: [],
		},
	],
	SETTINGS_AREAS: [],
}));

const { updateSettingsSectionMock } = vi.hoisted(() => ({
	updateSettingsSectionMock: vi.fn(),
}));

vi.mock('$lib/api', () => ({
	updateSettingsSection: updateSettingsSectionMock,
}));

import SettingsSaveBar from '../lib/components/settings/shell/SettingsSaveBar.svelte';
import { dirtyFields, originalValues, clearDirty } from '../lib/settings/dirty';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	target?.remove();
});

beforeEach(() => {
	clearDirty();
	originalValues.set({});
	updateSettingsSectionMock.mockReset();
	updateSettingsSectionMock.mockResolvedValue({ status: 'ok' });
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
	await Promise.resolve();
}

describe('SettingsSaveBar', () => {
	it('is hidden when no dirty fields', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSaveBar, {
			target,
			props: { currentValues: {} },
		});
		await flush();

		expect(target.textContent || '').not.toContain('unsaved');
	});

	it('shows count and Save all when dirty', async () => {
		dirtyFields.set(new Set(['risk.max_daily_loss', 'risk.max_drawdown_pct']));
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSaveBar, {
			target,
			props: {
				currentValues: {
					'risk.max_daily_loss': 150,
					'risk.max_drawdown_pct': 8,
				},
			},
		});
		await flush();

		expect(target.textContent).toContain('2 unsaved');
		expect(target.textContent).toContain('Save all');
	});

	it('clears dirty on successful Save all', async () => {
		dirtyFields.set(new Set(['risk.max_daily_loss']));
		originalValues.set({ 'risk.max_daily_loss': 200 });
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSaveBar, {
			target,
			props: { currentValues: { 'risk.max_daily_loss': 150 } },
		});
		await flush();

		const buttons = Array.from(target.querySelectorAll('button')) as HTMLButtonElement[];
		const saveBtn = buttons.find((b) => (b.textContent || '').includes('Save all'));
		expect(saveBtn).toBeTruthy();
		saveBtn!.click();
		await flush();
		await flush();

		expect(get(dirtyFields).size).toBe(0);
	});

	it('preserves in-flight edits made during save (Test D)', async () => {
		// Give the mock a pending promise whose resolver we capture.
		let pendingResolve: (v: unknown) => void = () => {};
		updateSettingsSectionMock.mockImplementationOnce(
			() =>
				new Promise((r) => {
					pendingResolve = r;
				}),
		);

		dirtyFields.set(new Set(['risk.max_daily_loss']));
		originalValues.set({ 'risk.max_daily_loss': 200 });
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSaveBar, {
			target,
			props: { currentValues: { 'risk.max_daily_loss': 150 } },
		});
		await flush();

		const buttons = Array.from(target.querySelectorAll('button')) as HTMLButtonElement[];
		const saveBtn = buttons.find((b) => (b.textContent || '').includes('Save all'));
		expect(saveBtn).toBeTruthy();
		saveBtn!.click();
		await flush();

		// User types a new edit while save is pending.
		dirtyFields.update((s) => new Set([...s, 'risk.max_drawdown_pct']));

		// Now resolve the update.
		expect(typeof pendingResolve).toBe('function');
		pendingResolve({ status: 'ok' });

		await flush();
		await flush();

		const finalDirty = get(dirtyFields);
		expect(finalDirty.has('risk.max_drawdown_pct')).toBe(true);
		expect(finalDirty.has('risk.max_daily_loss')).toBe(false);
	});

	it('short-circuits when grouping yields no sections (Test E)', async () => {
		dirtyFields.set(new Set(['orphan.unknown_id']));
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSaveBar, {
			target,
			props: { currentValues: { 'orphan.unknown_id': 123 } },
		});
		await flush();

		const buttons = Array.from(target.querySelectorAll('button')) as HTMLButtonElement[];
		const saveBtn = buttons.find((b) => (b.textContent || '').includes('Save all'));
		expect(saveBtn).toBeTruthy();
		saveBtn!.click();
		await flush();
		await flush();

		expect(updateSettingsSectionMock).not.toHaveBeenCalled();
		const dirty = get(dirtyFields);
		expect(dirty.has('orphan.unknown_id')).toBe(true);
		expect(dirty.size).toBe(1);
		expect(target.textContent || '').toContain('No saveable fields');
	});

	it('saves sections sequentially, never concurrently (same-KV lost-update guard)', async () => {
		// Every section handler read-modify-writes the same forven:settings KV
		// blob; two in-flight PUTs would race and silently drop one section.
		let active = 0;
		let maxActive = 0;
		const order: string[] = [];
		updateSettingsSectionMock.mockImplementation(async (section: string) => {
			active += 1;
			maxActive = Math.max(maxActive, active);
			order.push(section);
			await new Promise((r) => setTimeout(r, 5));
			active -= 1;
			return { status: 'ok' };
		});

		dirtyFields.set(new Set(['risk.max_daily_loss', 'research.strategy_creation_daily_budget']));
		originalValues.set({
			'risk.max_daily_loss': 200,
			'research.strategy_creation_daily_budget': 12,
		});
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSaveBar, {
			target,
			props: {
				currentValues: {
					'risk.max_daily_loss': 150,
					'research.strategy_creation_daily_budget': 6,
				},
			},
		});
		await flush();

		const buttons = Array.from(target.querySelectorAll('button')) as HTMLButtonElement[];
		const saveBtn = buttons.find((b) => (b.textContent || '').includes('Save all'));
		expect(saveBtn).toBeTruthy();
		saveBtn!.click();
		await new Promise((r) => setTimeout(r, 30));
		await flush();

		expect(updateSettingsSectionMock).toHaveBeenCalledTimes(2);
		expect(maxActive).toBe(1);
		expect(order.sort()).toEqual(['research', 'risk']);
		// The dotted research path produced the full nested body.
		expect(updateSettingsSectionMock).toHaveBeenCalledWith('research', {
			research_settings: { strategy_creation_daily_budget: 6 },
		});
		expect(get(dirtyFields).size).toBe(0);
	});

	it('refuses to save while a numeric field is empty (null) and names the field', async () => {
		dirtyFields.set(new Set(['risk.max_daily_loss', 'risk.max_drawdown_pct']));
		originalValues.set({ 'risk.max_daily_loss': 200, 'risk.max_drawdown_pct': 30 });
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsSaveBar, {
			target,
			props: {
				currentValues: {
					// Cleared number input — SettingsFieldRow marks null.
					'risk.max_daily_loss': null,
					'risk.max_drawdown_pct': 25,
				},
			},
		});
		await flush();

		const buttons = Array.from(target.querySelectorAll('button')) as HTMLButtonElement[];
		const saveBtn = buttons.find((b) => (b.textContent || '').includes('Save all'));
		expect(saveBtn).toBeTruthy();
		saveBtn!.click();
		await flush();
		await flush();

		// Nothing was submitted — not even the valid sibling (refuse the whole save).
		expect(updateSettingsSectionMock).not.toHaveBeenCalled();
		// Both fields stay dirty so nothing is silently lost.
		const dirty = get(dirtyFields);
		expect(dirty.has('risk.max_daily_loss')).toBe(true);
		expect(dirty.has('risk.max_drawdown_pct')).toBe(true);
		// The error names the offending field.
		expect(target.textContent || '').toContain("'Max daily loss' is empty");
	});
});
