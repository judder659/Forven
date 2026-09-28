// What changed between two rule specs, in words: used to review an AI edit
// before accepting it and to compare two saved runs.
import type { RuleSpec } from '$lib/components/strategy/templates';
import { sideToFormula } from './ruleFormula';

export interface SpecChange {
	kind: 'added' | 'removed' | 'changed';
	area: 'indicator' | 'knob' | 'rule';
	label: string;
	before?: string;
	after?: string;
}

const SIDE_LABELS: [keyof RuleSpec, string][] = [
	['entry_long', 'Enter long'],
	['exit_long', 'Exit long'],
	['entry_short', 'Enter short'],
	['exit_short', 'Exit short'],
];

function describeParams(params: Record<string, number> | undefined): string {
	return Object.entries(params ?? {}).map(([key, value]) => `${key} ${value}`).join(', ');
}

export function diffSpecs(before: RuleSpec | null | undefined, after: RuleSpec | null | undefined): SpecChange[] {
	const changes: SpecChange[] = [];
	const oldInds = new Map((before?.indicators ?? []).map((ind) => [ind.id, ind]));
	const newInds = new Map((after?.indicators ?? []).map((ind) => [ind.id, ind]));
	for (const [id, ind] of newInds) {
		const old = oldInds.get(id);
		if (!old) changes.push({ kind: 'added', area: 'indicator', label: id, after: `${ind.kind} (${describeParams(ind.params)})` });
		else if (old.kind !== ind.kind || describeParams(old.params) !== describeParams(ind.params)) {
			changes.push({ kind: 'changed', area: 'indicator', label: id,
				before: `${old.kind} (${describeParams(old.params)})`, after: `${ind.kind} (${describeParams(ind.params)})` });
		}
	}
	for (const [id, ind] of oldInds) {
		if (!newInds.has(id)) changes.push({ kind: 'removed', area: 'indicator', label: id, before: `${ind.kind} (${describeParams(ind.params)})` });
	}
	const oldParams = before?.params ?? {};
	const newParams = after?.params ?? {};
	for (const [name, value] of Object.entries(newParams)) {
		if (!(name in oldParams)) changes.push({ kind: 'added', area: 'knob', label: name, after: String(value) });
		else if (Number(oldParams[name]) !== Number(value)) {
			changes.push({ kind: 'changed', area: 'knob', label: name, before: String(oldParams[name]), after: String(value) });
		}
	}
	for (const name of Object.keys(oldParams)) {
		if (!(name in newParams)) changes.push({ kind: 'removed', area: 'knob', label: name, before: String(oldParams[name]) });
	}
	for (const [key, label] of SIDE_LABELS) {
		const was = sideToFormula(before?.[key] as RuleSpec['entry_long']);
		const now = sideToFormula(after?.[key] as RuleSpec['entry_long']);
		if (was === now) continue;
		changes.push({ kind: !was ? 'added' : !now ? 'removed' : 'changed', area: 'rule', label,
			...(was ? { before: was } : {}), ...(now ? { after: now } : {}) });
	}
	return changes;
}
