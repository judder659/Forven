import { describe, it, expect, afterEach } from 'vitest';
import { mount, unmount } from 'svelte';
import SettingsFieldRow from '../lib/components/settings/primitives/SettingsFieldRow.svelte';
import { SETTINGS_MANIFEST } from '../lib/settings/manifest';
import { boundedFieldIds, describeRange, numberBounds, outOfRange } from '../lib/settings/bounds';

let target: HTMLElement;
let instance: any;

afterEach(() => {
	if (instance) unmount(instance);
	instance = null;
	target?.remove();
});

async function flush(): Promise<void> {
	await Promise.resolve();
	await new Promise((r) => setTimeout(r, 0));
}

describe('settings number bounds', () => {
	it('covers every number field in the manifest, and nothing else', () => {
		const numberIds = SETTINGS_MANIFEST.filter((e) => e.type === 'number').map((e) => e.id).sort();
		expect(boundedFieldIds().sort()).toEqual(numberIds);
	});

	it('keeps every default inside its range', () => {
		for (const entry of SETTINGS_MANIFEST.filter((e) => e.type === 'number')) {
			expect(outOfRange(entry.default, numberBounds(entry.id)), entry.id).toBe(false);
		}
	});

	it('describes ranges in words', () => {
		expect(describeRange({ min: 0, max: 100, step: 'any' })).toBe('0 to 100');
		expect(describeRange({ min: 1, step: 1 })).toBe('at least 1');
		expect(describeRange({ step: 'any' })).toBe('');
	});

	it('puts the range on the input and warns when a value is outside it', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'risk.live_max_leverage',
				label: 'Max leverage (mainnet)',
				description: 'Leverage ceiling.',
				unit: 'x',
				defaultValue: 3,
				value: 5,
				type: 'number',
			},
		});
		await flush();
		const input = target.querySelector('input[type="number"]') as HTMLInputElement;
		expect(input.min).toBe('1');
		expect(input.max).toBe('3');
		expect(target.querySelector('[data-testid="range-risk.live_max_leverage"]')?.textContent).toBe('1 to 3 x');
		const warning = target.querySelector('[data-testid="out-of-range-risk.live_max_leverage"]');
		expect(warning?.textContent).toContain('Must be 1 to 3 x');
		expect(warning?.textContent).toContain('will not keep');
	});
});
