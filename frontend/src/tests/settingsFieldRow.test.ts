import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import SettingsFieldRow from '../lib/components/settings/primitives/SettingsFieldRow.svelte';
import { dirtyFields, originalValues, clearDirty } from '../lib/settings/dirty';
import * as dirtyModule from '../lib/settings/dirty';

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

describe('SettingsFieldRow', () => {
	it('renders label, description, unit, default, and setting id', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'risk.max_daily_loss',
				label: 'Max daily loss',
				description: 'Loss before new entries are blocked for the day.',
				unit: '$',
				defaultValue: 200,
				value: 200,
				type: 'number',
			},
		});
		await flush();

		expect(target.textContent).toContain('Max daily loss');
		expect(target.textContent).toContain('Loss before new entries');
		expect(target.textContent).toContain('$');
		expect(target.textContent).toContain('Default: 200');
		expect(target.textContent).toContain('risk.max_daily_loss');
	});

	it('shows dirty dot when value changes from original', async () => {
		originalValues.set({ 'risk.max_daily_loss': 200 });
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'risk.max_daily_loss',
				label: 'Max daily loss',
				description: '.',
				defaultValue: 200,
				value: 200,
				type: 'number',
			},
		});
		await flush();

		// No dirty dot yet.
		expect(target.querySelector('[data-testid="dirty-dot-risk.max_daily_loss"]')).toBeNull();

		const input = target.querySelector('input[type="number"]') as HTMLInputElement;
		expect(input).toBeTruthy();
		input.value = '150';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		await flush();

		expect(get(dirtyFields).has('risk.max_daily_loss')).toBe(true);
		expect(target.querySelector('[data-testid="dirty-dot-risk.max_daily_loss"]')).toBeTruthy();
	});

	it('renders a checkbox for toggle type', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'agents.allow_auto_retry',
				label: 'Allow auto retry',
				description: '.',
				defaultValue: false,
				value: true,
				type: 'toggle',
			},
		});
		await flush();

		const checkbox = target.querySelector('input[type="checkbox"]') as HTMLInputElement;
		expect(checkbox).toBeTruthy();
		expect(checkbox.checked).toBe(true);
	});

	it('renders options for select type', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'trading_mode',
				label: 'Trading mode',
				description: '.',
				defaultValue: 'paper',
				value: 'paper',
				type: 'select',
				options: [
					{ value: 'paper', label: 'Paper' },
					{ value: 'live', label: 'Live' },
				],
			},
		});
		await flush();

		const select = target.querySelector('select') as HTMLSelectElement;
		expect(select).toBeTruthy();
		const opts = Array.from(select.querySelectorAll('option')).map((o) => o.textContent?.trim());
		expect(opts).toEqual(['Paper', 'Live']);
	});

	it('renders a password input for secret type', async () => {
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'api.openai_key',
				label: 'OpenAI key',
				description: '.',
				defaultValue: '',
				value: 'sk-test',
				type: 'secret',
			},
		});
		await flush();

		const input = target.querySelector('input[type="password"]') as HTMLInputElement;
		expect(input).toBeTruthy();
	});

	it('passes null to markField when a number input is cleared (no silent 0 coercion)', async () => {
		const spy = vi.spyOn(dirtyModule, 'markField');
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'risk.max_daily_loss',
				label: 'Max daily loss',
				description: '.',
				defaultValue: 200,
				value: 200,
				type: 'number',
			},
		});
		await flush();

		const input = target.querySelector('input[type="number"]') as HTMLInputElement;
		expect(input).toBeTruthy();
		input.value = '';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		await flush();

		expect(spy).toHaveBeenCalledWith('risk.max_daily_loss', null);
		expect(spy).not.toHaveBeenCalledWith('risk.max_daily_loss', 0);
		spy.mockRestore();
	});

	it('csv type renders array joined with commas and parses input back to an array', async () => {
		const spy = vi.spyOn(dirtyModule, 'markField');
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'agent-model-keys.agent_model_keys',
				label: 'Enabled model options',
				description: '.',
				defaultValue: [],
				value: ['openai:gpt-4o', 'anthropic:claude-opus-4-7'],
				type: 'csv',
			},
		});
		await flush();

		const input = target.querySelector('input[type="text"]') as HTMLInputElement;
		expect(input).toBeTruthy();
		expect(input.value).toBe('openai:gpt-4o, anthropic:claude-opus-4-7');

		input.value = 'openai:gpt-4o, groq:llama-3';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		await flush();

		expect(spy).toHaveBeenCalledWith(
			'agent-model-keys.agent_model_keys',
			['openai:gpt-4o', 'groq:llama-3'],
		);
		spy.mockRestore();
	});

	it('csv type drops empty segments and trims whitespace', async () => {
		const spy = vi.spyOn(dirtyModule, 'markField');
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'agent-model-keys.agent_model_keys',
				label: 'Enabled model options',
				description: '.',
				defaultValue: [],
				value: [],
				type: 'csv',
			},
		});
		await flush();

		const input = target.querySelector('input[type="text"]') as HTMLInputElement;
		input.value = ' a ,  , b,';
		input.dispatchEvent(new Event('input', { bubbles: true }));
		await flush();

		expect(spy).toHaveBeenCalledWith('agent-model-keys.agent_model_keys', ['a', 'b']);
		spy.mockRestore();
	});

	it('csv type with options renders checkboxes and saves selected values in option order', async () => {
		const spy = vi.spyOn(dirtyModule, 'markField');
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'test.csv_field',
				label: 'CSV field',
				description: '.',
				defaultValue: ['candles'],
				value: ['oi', 'candles'],
				type: 'csv',
				options: [
					{ value: 'candles', label: 'Candles' },
					{ value: 'funding', label: 'Funding' },
					{ value: 'oi', label: 'Open interest' },
				],
			},
		});
		await flush();

		const checkboxes = Array.from(target.querySelectorAll('input[type="checkbox"]')) as HTMLInputElement[];
		expect(checkboxes).toHaveLength(3);
		expect(checkboxes.map((input) => input.checked)).toEqual([true, false, true]);

		checkboxes[1].checked = true;
		checkboxes[1].dispatchEvent(new Event('change', { bubbles: true }));
		await flush();

		expect(spy).toHaveBeenCalledWith('test.csv_field', ['candles', 'funding', 'oi']);
		spy.mockRestore();
	});

	it('warns via console.warn when a select mounts with an unmatched value', async () => {
		const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
		target = document.createElement('div');
		document.body.appendChild(target);
		instance = mount(SettingsFieldRow, {
			target,
			props: {
				id: 'exchange',
				label: 'Exchange',
				description: '.',
				defaultValue: 'a',
				value: 'zz',
				type: 'select',
				options: [
					{ value: 'a', label: 'A' },
					{ value: 'b', label: 'B' },
				],
			},
		});
		await flush();

		expect(warnSpy).toHaveBeenCalledTimes(1);
		const message = String(warnSpy.mock.calls[0][0]);
		expect(message).toContain('exchange');
		expect(message).toContain('zz');
		warnSpy.mockRestore();
	});
});
