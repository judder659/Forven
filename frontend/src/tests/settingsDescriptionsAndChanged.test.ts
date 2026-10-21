import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

const profileApi = vi.hoisted(() => ({
	getProfile: vi.fn(),
	putProfile: vi.fn(),
}));
vi.mock('$lib/api/profile', () => profileApi);

import SettingsFieldRow from '../lib/components/settings/primitives/SettingsFieldRow.svelte';
import ChangedSettingsView from '../lib/components/settings/ChangedSettingsView.svelte';
import AssistantProfileBlock from '../lib/components/settings/blocks/AssistantProfileBlock.svelte';
import { SETTINGS_MANIFEST } from '../lib/settings/manifest';
import { clearDirty, originalValues } from '../lib/settings/dirty';

let target: HTMLElement;
let instance: ReturnType<typeof mount> | null = null;

async function flush(): Promise<void> {
	for (let i = 0; i < 4; i += 1) {
		await Promise.resolve();
		await new Promise((r) => setTimeout(r, 0));
	}
	flushSync();
}

function render(component: any, props: Record<string, unknown>): void {
	target = document.createElement('div');
	document.body.appendChild(target);
	instance = mount(component, { target, props });
}

beforeEach(() => {
	clearDirty();
	originalValues.set({});
});

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target?.remove();
	vi.clearAllMocks();
});

describe('setting descriptions behind (?)', () => {
	it('hides the description until the (?) is clicked', async () => {
		render(SettingsFieldRow, {
			id: 'risk.max_daily_loss',
			label: 'Max daily loss',
			description: 'Loss before new entries are blocked for the day.',
			defaultValue: 200,
			value: 200,
			type: 'number',
		});
		await flush();
		const help = target.querySelector('[data-testid="help-text-risk.max_daily_loss"]') as HTMLElement;
		expect(help.className).toContain('hidden');
		expect(help.textContent).toContain('Default: 200');

		(target.querySelector('[data-testid="help-risk.max_daily_loss"]') as HTMLButtonElement).click();
		await flush();
		expect(help.className).not.toContain('hidden');
		expect(help.textContent).toContain('Loss before new entries');
	});

	it('shows the reset line only when a field differs from its default', async () => {
		render(SettingsFieldRow, {
			id: 'risk.max_daily_loss',
			label: 'Max daily loss',
			description: 'x',
			defaultValue: 200,
			value: 300,
			type: 'number',
		});
		await flush();
		expect(target.querySelector('[data-testid="changed-risk.max_daily_loss"]')?.textContent).toContain('Changed from 200');
		expect(target.querySelector('[data-testid="reset-risk.max_daily_loss"]')).not.toBeNull();
	});
});

describe('Only what I changed', () => {
	it('says everything is at its default when nothing differs', async () => {
		render(ChangedSettingsView, { settings: {} });
		await flush();
		expect(target.querySelector('[data-testid="changed-settings-empty"]')).not.toBeNull();
	});

	it('lists only the fields that differ, grouped by area', async () => {
		const entry = SETTINGS_MANIFEST.find((e) => e.id === 'pipeline.db_backup_keep_daily')!;
		render(ChangedSettingsView, { settings: { [entry.backendPath]: 14 } });
		await flush();
		expect(target.querySelector('[data-testid="changed-area-data"]')).not.toBeNull();
		expect(target.querySelector('[data-testid="changed-area-trading"]')).toBeNull();
		const rows = target.querySelectorAll('[data-testid^="help-text-"]');
		expect(rows).toHaveLength(1);
		expect(target.querySelector(`[data-testid="help-text-${entry.id}"]`)).not.toBeNull();
	});
});

describe('graduation recommender', () => {
	it('shows its fields only while it is switched on', () => {
		const group = SETTINGS_MANIFEST.filter((e) => e.subsection === 'lab-graduation-recommender');
		expect(group.length).toBeGreaterThan(1);
		for (const e of group) {
			expect(e.showWhen).toEqual({ id: 'risk.live_graduation_recommender_enabled', equals: [true] });
		}
	});
});

describe('What the assistant knows about you', () => {
	it('loads the profile and saves edits', async () => {
		const loaded = {
			exists: true,
			parse_error: null,
			structured: { name: 'Jo', timezone: 'UTC', rules: ['no live without a backtest'], preferences: {} },
			body: 'notes',
		};
		profileApi.getProfile.mockResolvedValue(loaded);
		profileApi.putProfile.mockResolvedValue(loaded);
		render(AssistantProfileBlock, {});
		await flush();

		const save = target.querySelector('[data-testid="assistant-profile-save"]') as HTMLButtonElement;
		expect(save.disabled).toBe(true);
		const name = target.querySelector('input[placeholder="Your name"]') as HTMLInputElement;
		expect(name.value).toBe('Jo');
		name.value = 'Jo B';
		name.dispatchEvent(new Event('input'));
		await flush();
		expect(save.disabled).toBe(false);
		save.click();
		await flush();
		expect(profileApi.putProfile).toHaveBeenCalledWith(
			expect.objectContaining({ structured: expect.objectContaining({ name: 'Jo B', rules: ['no live without a backtest'] }) }),
		);
	});
});
