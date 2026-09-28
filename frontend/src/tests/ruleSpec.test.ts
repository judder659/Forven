import { describe, expect, it } from 'vitest';
import { builderIncompatibility } from '$lib/utils/ruleSpec';

const c = (left: unknown, right: unknown = 1) => ({ left, op: '>', right });

describe('builderIncompatibility', () => {
	it('accepts every operand spelling the engine reads and one level of groups', () => {
		expect(builderIncompatibility({
			entry_long: { logic: 'and', conditions: [
				c('close'), c(1, { param: 'x' }), c({ series: 'rsi' }, { const: 2 }),
				{ logic: 'or', conditions: [c({ indicator: 'macd' }, 'macd_signal')] },
			] },
			exit_long: null,
		})).toBeNull();
	});

	it('refuses groups nested deeper than the builder shows', () => {
		const deep = { logic: 'or', conditions: [c('close'), { logic: 'and', conditions: [c('open'), c('high')] }] };
		expect(builderIncompatibility({ entry_long: { logic: 'and', conditions: [c('low'), deep] } }))
			.toMatch(/Entry Long nests condition groups more than one level deep/);
	});

	it('refuses an operand it cannot show', () => {
		expect(builderIncompatibility({ exit_short: { conditions: [c({ lag: ['close', 1] })] } }))
			.toMatch(/Exit Short has a condition/);
		expect(builderIncompatibility(null)).toMatch(/no rule spec/);
	});
});
