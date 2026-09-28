import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mount, unmount, tick } from 'svelte';
import { fireEvent } from '@testing-library/svelte';
import StrategyBuilder from '../lib/components/strategy/StrategyBuilder.svelte';
import type { IndicatorMeta } from '$lib/api';
import type { RuleSpec } from '$lib/components/strategy/templates';

const META: IndicatorMeta[] = [
	{
		kind: 'rsi', label: 'RSI', category: 'Momentum', description: '', panel: 'sub', multi_output: false,
		params: [{ key: 'length', type: 'number', default: 14, min: 2, max: 100, step: 1 }], output_suffixes: [''],
	},
	{
		kind: 'bollinger', label: 'Bollinger Bands', category: 'Volatility', description: '', panel: 'main', multi_output: true,
		params: [
			{ key: 'length', type: 'number', default: 20, min: 2, max: 400, step: 1 },
			{ key: 'num_std', type: 'number', default: 2, min: 0.5, max: 5, step: 0.1 },
		],
		output_suffixes: ['', '_mid', '_upper', '_lower'],
	},
];

type Change = { spec: Record<string, any>; valid: boolean; errors: string[] };
let target: HTMLDivElement;
let app: ReturnType<typeof mount>;
let last: Change;

async function settle() {
	for (let i = 0; i < 6; i++) { await Promise.resolve(); await tick(); }
}
async function open(spec: RuleSpec | Record<string, unknown>) {
	app = mount(StrategyBuilder, {
		target,
		props: { indicators: META, initialSpec: spec as RuleSpec },
		events: { change: (e: CustomEvent<Change>) => { last = e.detail; } },
	});
	await settle();
}
function inputs(label: string) {
	return [...target.querySelectorAll(`input[aria-label="${label}"]`)] as HTMLInputElement[];
}
function buttons(text: string | RegExp) {
	return [...target.querySelectorAll('button')].filter((b) =>
		typeof text === 'string' ? b.textContent?.trim() === text : text.test(b.textContent ?? ''));
}
async function type(input: HTMLInputElement, value: string) {
	await fireEvent.input(input, { target: { value } });
	await settle();
}
const cond = (left: unknown, op: string, right: unknown) => ({ left, op, right });

beforeEach(() => { target = document.createElement('div'); document.body.appendChild(target); });
afterEach(async () => { await unmount(app); target.remove(); });

describe('StrategyBuilder', () => {
	it('keeps {series} and {indicator} operands as series instead of the constant 0', async () => {
		await open({
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }], params: {},
			entry_long: { logic: 'and', conditions: [cond({ indicator: 'rsi' }, '<', 30)] },
			exit_long: { logic: 'or', conditions: [cond({ series: 'close' }, '>', { const: 100 })] },
			entry_short: null, exit_short: null,
		});
		expect(last.valid).toBe(true);
		expect(last.spec.entry_long.conditions[0]).toEqual(cond('rsi', '<', 30));
		expect(last.spec.exit_long.conditions[0]).toEqual(cond('close', '>', 100));
	});

	it('renaming an indicator re-points every output its conditions use', async () => {
		await open({
			indicators: [{ id: 'bb', kind: 'bollinger', params: { length: 20, num_std: 2 } }], params: {},
			entry_long: { logic: 'and', conditions: [cond('close', 'crosses_above', 'bb_upper')] },
			exit_long: { logic: 'or', conditions: [cond('close', 'crosses_below', 'bb_mid')] },
			entry_short: null, exit_short: null,
		});
		await type(inputs('indicator id')[0], 'band');
		expect(last.valid).toBe(true);
		expect(last.spec.indicators[0].id).toBe('band');
		expect(last.spec.entry_long.conditions[0].right).toBe('band_upper');
		expect(last.spec.exit_long.conditions[0].right).toBe('band_mid');
	});

	it('renaming a parameter re-points the conditions that read it', async () => {
		await open({
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }], params: { oversold: 30 },
			entry_long: { logic: 'and', conditions: [cond('rsi', '<', { param: 'oversold' })] },
			exit_long: null, entry_short: null, exit_short: null,
		});
		await type(inputs('parameter name')[0], 'dip');
		expect(last.valid).toBe(true);
		expect(last.spec.params).toEqual({ dip: 30 });
		expect(last.spec.entry_long.conditions[0].right).toEqual({ param: 'dip' });
	});

	it('does not merge references when a rename passes through a name already in use', async () => {
		await open({
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }, { id: 'slow', kind: 'rsi', params: { length: 50 } }],
			params: {},
			entry_long: { logic: 'and', conditions: [cond('rsi', '<', 30)] },
			exit_long: { logic: 'or', conditions: [cond('slow', '>', 70)] },
			entry_short: null, exit_short: null,
		});
		const slow = inputs('indicator id')[1];
		await type(slow, 'rsi');
		expect(last.errors).toContain('Duplicate indicator id "rsi".');
		await type(slow, 'rsi_slow');
		expect(last.valid).toBe(true);
		expect(last.spec.entry_long.conditions[0].left).toBe('rsi');
		expect(last.spec.exit_long.conditions[0].left).toBe('rsi_slow');
	});

	it('rejects settings the engine would silently replace', async () => {
		await open({
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 1 } }], params: { leverage: 2, oversold: 30, dip: 25 },
			entry_long: { logic: 'and', conditions: [cond('rsi', '<', { param: 'oversold' })] },
			exit_long: null, entry_short: null, exit_short: null,
		});
		expect(last.valid).toBe(false);
		expect(last.errors).toContain('RSI length must be at least 2.');
		expect(last.errors).toContain('Parameter name "leverage" is reserved for a strategy setting. Choose another name.');

		await type(inputs('parameter name')[2], 'oversold');
		expect(last.errors).toContain('Duplicate parameter "oversold".');
		expect(last.spec.entry_long.conditions[0].right).toEqual({ param: 'oversold' });

		const length = [...target.querySelectorAll('label')].find((l) => l.textContent?.trim() === 'length')!.querySelector('input')!;
		await type(length, '');
		expect(last.errors).toContain('RSI length needs a number.');
	});

	it('turns a number into a knob named after what it is compared with', async () => {
		await open({
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }], params: {},
			entry_long: { logic: 'and', conditions: [cond('rsi', '<', 30)] },
			exit_long: null, entry_short: null, exit_short: null,
		});
		await fireEvent.click(target.querySelector('button[aria-label="right operand"]')!); await settle();
		await fireEvent.click(buttons('Knob')[0]); await settle();
		await fireEvent.click(buttons(/Turn 30 into a knob/)[0]); await settle();
		expect(last.valid).toBe(true);
		expect(last.spec.params).toEqual({ rsi_level: 30 });
		expect(last.spec.entry_long.conditions[0].right).toEqual({ param: 'rsi_level' });
	});

	it('points the condition that asked for a new indicator at it', async () => {
		await open({
			indicators: [], params: {},
			entry_long: { logic: 'and', conditions: [cond('close', '>', 0)] },
			exit_long: null, entry_short: null, exit_short: null,
		});
		await fireEvent.click(target.querySelector('button[aria-label="right operand"]')!); await settle();
		await fireEvent.click(buttons('Series')[0]); await settle();
		await fireEvent.click(buttons(/Add an indicator/)[0]); await settle();
		const option = [...document.querySelectorAll('[aria-label="Add an indicator"] [role="option"]')]
			.find((b) => b.textContent?.includes('Bollinger Bands'))!;
		await fireEvent.click(option); await settle();
		expect(last.valid).toBe(true);
		expect(last.spec.indicators).toEqual([{ id: 'bollinger', kind: 'bollinger', params: { length: 20, num_std: 2 } }]);
		expect(last.spec.entry_long.conditions[0].right).toBe('bollinger');
	});

	it('applies a formula and refuses one that reads an unknown series', async () => {
		await open({
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }], params: { oversold: 30 },
			entry_long: { logic: 'and', conditions: [cond('rsi', '<', { param: 'oversold' })] },
			exit_long: null, entry_short: null, exit_short: null,
		});
		await fireEvent.click(target.querySelector('button[aria-label="edit as formula"]')!); await settle();
		const area = target.querySelector('textarea[aria-label="Enter long formula"]') as HTMLTextAreaElement;
		expect(area.value).toBe('rsi < $oversold');

		await fireEvent.input(area, { target: { value: 'rsi < $oversold and close > sma50' } }); await settle();
		await fireEvent.click(buttons('Apply')[0]); await settle();
		expect(target.textContent).toContain('Unknown series "sma50"');
		expect(last.spec.entry_long.conditions).toHaveLength(1);

		await fireEvent.input(area, { target: { value: 'rsi crosses above $oversold or (close > open and volume > 0)' } }); await settle();
		await fireEvent.click(buttons('Apply')[0]); await settle();
		expect(last.valid).toBe(true);
		expect(last.spec.entry_long).toEqual({
			logic: 'or',
			conditions: [
				cond('rsi', 'crosses_above', { param: 'oversold' }),
				{ logic: 'and', conditions: [cond('close', '>', 'open'), cond('volume', '>', 0)] },
			],
		});
	});

	it('tunes a knob with its slider without moving the slider range', async () => {
		await open({
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }], params: { oversold: 30 },
			entry_long: { logic: 'and', conditions: [cond('rsi', '<', { param: 'oversold' })] },
			exit_long: null, entry_short: null, exit_short: null,
		});
		const slider = target.querySelector('input[type="range"]') as HTMLInputElement;
		expect([slider.min, slider.max]).toEqual(['0', '60']);
		await type(slider, '25');
		expect(last.spec.params).toEqual({ oversold: 25 });
		expect([slider.min, slider.max]).toEqual(['0', '60']);
	});
});
