import { describe, expect, it } from 'vitest';
import { formulaToSide, sideToFormula } from '$lib/utils/ruleFormula';
import { diffSpecs } from '$lib/utils/specDiff';
import { indicatorLabel, seriesLabel } from '$lib/utils/ruleLabels';
import { STRATEGY_TEMPLATES } from '$lib/components/strategy/templates';
import type { IndicatorMeta } from '$lib/api';

const c = (left: unknown, op: string, right: unknown) => ({ left, op, right });

describe('rule formulas', () => {
	it('round-trips every template side', () => {
		for (const template of STRATEGY_TEMPLATES) {
			for (const key of ['entry_long', 'exit_long', 'entry_short', 'exit_short'] as const) {
				const side = template.spec[key];
				const text = sideToFormula(side);
				const parsed = formulaToSide(text, side?.logic ?? 'and');
				expect(parsed.error).toBeNull();
				expect(sideToFormula(parsed.side)).toBe(text);
			}
		}
	});

	it('reads parameters, numbers, word operators and one level of groups', () => {
		const { side, error } = formulaToSide('rsi crosses below $oversold and (close > ema200 or macd >= -0.5)');
		expect(error).toBeNull();
		expect(side).toEqual({
			logic: 'and',
			conditions: [
				c('rsi', 'crosses_below', { param: 'oversold' }),
				{ logic: 'or', conditions: [c('close', '>', 'ema200'), c('macd', '>=', -0.5)] },
			],
		});
		expect(sideToFormula(side)).toBe('rsi crosses below $oversold and (close > ema200 or macd >= -0.5)');
	});

	it('gives "and" precedence over "or"', () => {
		const { side } = formulaToSide('a > 1 or b > 2 and c > 3');
		expect(side).toEqual({ logic: 'or', conditions: [c('a', '>', 1), { logic: 'and', conditions: [c('b', '>', 2), c('c', '>', 3)] }] });
	});

	it('accepts symbol spellings and keeps the side logic for a single condition', () => {
		expect(formulaToSide('rsi ≥ 70', 'or').side).toEqual({ logic: 'or', conditions: [c('rsi', '>=', 70)] });
		expect(formulaToSide('close = open').side?.conditions[0]).toEqual(c('close', '==', 'open'));
		expect(formulaToSide('fast crosses_above slow').side?.conditions[0]).toEqual(c('fast', 'crosses_above', 'slow'));
		expect(formulaToSide('   ')).toEqual({ side: null, error: null, at: null });
	});

	it('reports where a formula goes wrong', () => {
		expect(formulaToSide('rsi <').error).toBe('A value is missing');
		expect(formulaToSide('rsi crosses 30')).toMatchObject({ error: 'Write "crosses above" or "crosses below"' });
		expect(formulaToSide('(rsi < 30').error).toBe('Missing ")"');
		expect(formulaToSide('rsi < 30 #').at).toBe(9);
		expect(formulaToSide('a > 1 and (b > 2 or (c > 3 and d > 4))').error).toMatch(/one level deep/);
	});
});

describe('spec diffs', () => {
	it('names added, removed and changed parts', () => {
		const before = { ...STRATEGY_TEMPLATES[0].spec };
		const after = {
			...before,
			indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 10 } }, { id: 'ema200', kind: 'ema', params: { length: 200 } }],
			params: { oversold: 25 },
			entry_long: { logic: 'and' as const, conditions: [c('rsi', '<', { param: 'oversold' }), c('close', '>', 'ema200')] },
		};
		const changes = diffSpecs(before, after as never);
		expect(changes).toContainEqual({ kind: 'changed', area: 'indicator', label: 'rsi', before: 'rsi (length 14)', after: 'rsi (length 10)' });
		expect(changes).toContainEqual({ kind: 'added', area: 'indicator', label: 'ema200', after: 'ema (length 200)' });
		expect(changes).toContainEqual({ kind: 'changed', area: 'knob', label: 'oversold', before: '30', after: '25' });
		expect(changes).toContainEqual({ kind: 'removed', area: 'knob', label: 'exit_level', before: '55' });
		expect(changes).toContainEqual({ kind: 'changed', area: 'rule', label: 'Enter long',
			before: 'rsi < $oversold', after: 'rsi < $oversold and close > ema200' });
		expect(diffSpecs(before, before)).toEqual([]);
	});
});

describe('rule labels', () => {
	const bollinger = { kind: 'bollinger', label: 'Bollinger Bands', category: 'Volatility', description: '', panel: 'main',
		multi_output: true, output_suffixes: ['', '_mid', '_upper', '_lower'],
		params: [{ key: 'length', type: 'number', default: 20, min: 2, max: 400, step: 1 }, { key: 'num_std', type: 'number', default: 2, min: 0.5, max: 5, step: 0.1 }] } as IndicatorMeta;

	it('names indicators by their settings and outputs by their role', () => {
		const bb = { id: 'bb', kind: 'bollinger', params: { length: 20, num_std: 2.5 } };
		expect(indicatorLabel(bb, bollinger)).toBe('Bollinger Bands(20, 2.5)');
		expect(seriesLabel('bb_upper', [bb], { bollinger })).toBe('Bollinger Bands(20, 2.5) upper');
		expect(seriesLabel('funding_rate', [], {})).toBe('Funding rate');
		expect(seriesLabel('mystery', [], {})).toBe('mystery');
	});
});
