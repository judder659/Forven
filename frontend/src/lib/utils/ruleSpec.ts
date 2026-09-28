// What the visual Strategy Builder can show of a rule_engine spec. The engine
// accepts condition groups nested to any depth and a few operand spellings; the
// builder shows a side's conditions plus one level of groups. Loading a spec it
// cannot show would silently rewrite the strategy, so callers check first.

const SIDES: [string, string][] = [
	['entry_long', 'Entry Long'],
	['exit_long', 'Exit Long'],
	['entry_short', 'Entry Short'],
	['exit_short', 'Exit Short'],
];

// A spec param with one of these names would be overwritten by the strategy
// setting of the same name that a saved strategy carries next to its spec.
export const RESERVED_PARAM_NAMES = [
	'spec', 'name', 'leverage', 'trade_mode', 'execution_profile', '_asset', '_creator_context',
];

function isObject(value: unknown): value is Record<string, unknown> {
	return !!value && typeof value === 'object' && !Array.isArray(value);
}

function isGroup(value: unknown): value is { conditions: unknown[] } {
	return isObject(value) && Array.isArray(value.conditions);
}

function operandShown(operand: unknown): boolean {
	if (typeof operand === 'number' || typeof operand === 'string') return true;
	return isObject(operand) && ['param', 'const', 'series', 'indicator'].some((key) => key in operand);
}

function conditionShown(condition: unknown): boolean {
	return isObject(condition) && operandShown(condition.left) && operandShown(condition.right);
}

/** Why the builder cannot show this spec as it is, or null when it can. */
export function builderIncompatibility(spec: unknown): string | null {
	if (!isObject(spec)) return 'There is no rule spec to open.';
	for (const [key, label] of SIDES) {
		const tree = spec[key];
		if (!isGroup(tree)) continue;
		for (const item of tree.conditions) {
			const conditions = isGroup(item) ? item.conditions : [item];
			if (conditions.some(isGroup)) {
				return `${label} nests condition groups more than one level deep, which the visual builder cannot show.`;
			}
			if (!conditions.every(conditionShown)) {
				return `${label} has a condition the visual builder cannot show.`;
			}
		}
	}
	return null;
}
