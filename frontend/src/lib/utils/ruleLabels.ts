// Human names for the parts of a rule spec, so rules read as sentences:
// "RSI(14) crosses below oversold (30)" instead of `rsi crosses_below $oversold`.
import type { IndicatorMeta } from '$lib/api';

export const RAW_COLUMN_LABELS: Record<string, string> = {
	close: 'Close',
	open: 'Open',
	high: 'High',
	low: 'Low',
	volume: 'Volume',
	funding_rate: 'Funding rate',
	open_interest: 'Open interest',
	taker_buy_sell_ratio: 'Taker buy/sell',
	ls_ratio: 'Long/short ratio',
	long_liq_usd: 'Long liquidations',
	short_liq_usd: 'Short liquidations',
	liq_imbalance: 'Liquidation imbalance',
};

export const OPERATOR_LABELS: Record<string, string> = {
	'<': 'is below',
	'<=': 'is at or below',
	'>': 'is above',
	'>=': 'is at or above',
	'==': 'equals',
	'!=': 'is not',
	crosses_above: 'crosses above',
	crosses_below: 'crosses below',
};

export interface IndicatorInstance {
	id: string;
	kind: string;
	params: Record<string, number>;
}

function formatParam(value: unknown): string {
	const n = Number(value);
	return Number.isFinite(n) ? String(+n.toPrecision(6)) : String(value ?? '');
}

/** "RSI(14)", "Bollinger Bands(20, 2)". */
export function indicatorLabel(instance: IndicatorInstance, meta: IndicatorMeta | undefined): string {
	if (!meta) return instance.id;
	const args = meta.params.map((p) => formatParam(instance.params?.[p.key] ?? p.default));
	return args.length ? `${meta.label}(${args.join(', ')})` : meta.label;
}

/** Label for a series name: a raw column or an indicator output ("BB(20, 2) upper"). */
export function seriesLabel(name: string, instances: IndicatorInstance[], metaByKind: Record<string, IndicatorMeta>): string {
	if (RAW_COLUMN_LABELS[name]) return RAW_COLUMN_LABELS[name];
	for (const instance of instances) {
		const meta = metaByKind[instance.kind];
		const suffixes = meta?.output_suffixes ?? [''];
		const id = instance.id.trim();
		for (const suffix of suffixes) {
			if (`${id}${suffix}` !== name) continue;
			const base = indicatorLabel(instance, meta);
			return suffix ? `${base} ${suffix.replace(/^_/, '').replaceAll('_', ' ')}` : base;
		}
	}
	return name;
}

export function formatValue(value: number | null | undefined): string {
	if (value == null || !Number.isFinite(value)) return '—';
	const abs = Math.abs(value);
	if (abs >= 1000) return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
	if (abs >= 1) return String(+value.toFixed(4));
	return String(+value.toPrecision(4));
}

type SpecLike = { indicators?: IndicatorInstance[]; params?: Record<string, number> } & Record<string, unknown>;

function asSpec(spec: unknown): SpecLike {
	return spec && typeof spec === 'object' ? (spec as SpecLike) : {};
}

/** Display names for every series a spec can read, by series name and by indicator id. */
export function specSeriesLabels(spec: unknown, metaByKind: Record<string, IndicatorMeta>): Record<string, string> {
	const labels: Record<string, string> = { ...RAW_COLUMN_LABELS };
	const instances = asSpec(spec).indicators ?? [];
	for (const instance of instances) {
		const meta = metaByKind[instance.kind];
		labels[instance.id] = indicatorLabel(instance, meta);
		for (const suffix of meta?.output_suffixes ?? ['']) {
			const name = `${instance.id}${suffix}`;
			labels[name] = seriesLabel(name, instances, metaByKind);
		}
	}
	return labels;
}

/** An operand as text: a series label, "oversold (30)", or a number. */
export function operandDisplay(operand: unknown, labels: Record<string, string>, knobs: Record<string, number> = {}): string {
	if (typeof operand === 'number') return formatValue(operand);
	if (typeof operand === 'string') return labels[operand] ?? operand;
	if (operand && typeof operand === 'object') {
		const obj = operand as Record<string, unknown>;
		if ('param' in obj) {
			const name = String(obj.param);
			return name in knobs ? `${name} (${formatValue(Number(knobs[name]))})` : name;
		}
		if ('const' in obj) return formatValue(Number(obj.const));
		const ref = obj.indicator ?? obj.series;
		if (ref != null) return labels[String(ref).trim()] ?? String(ref);
	}
	return String(operand ?? '');
}

/** The fixed levels a spec's rules compare sub-pane indicators with, keyed by
 * indicator id: `rsi < 30` draws a line at 30 on the RSI pane. */
export function specThresholds(
	value: unknown,
	metaByKind: Record<string, IndicatorMeta>,
): Record<string, Array<{ value: number; label: string }>> {
	const spec = asSpec(value);
	const owner = new Map<string, string>();
	for (const instance of spec.indicators ?? []) {
		const meta = metaByKind[instance.kind];
		if (meta?.panel !== 'sub') continue;
		for (const suffix of meta.output_suffixes ?? ['']) owner.set(`${instance.id}${suffix}`, instance.id);
	}
	const knobs = spec.params ?? {};
	const level = (operand: unknown): { value: number; label: string } | null => {
		if (typeof operand === 'number') return { value: operand, label: '' };
		if (operand && typeof operand === 'object') {
			const obj = operand as Record<string, unknown>;
			if ('const' in obj) return { value: Number(obj.const), label: '' };
			if ('param' in obj && String(obj.param) in knobs) return { value: Number(knobs[String(obj.param)]), label: String(obj.param) };
		}
		return null;
	};
	const seriesName = (operand: unknown): string | null => {
		if (typeof operand === 'string') return operand;
		if (operand && typeof operand === 'object') {
			const ref = (operand as Record<string, unknown>).indicator ?? (operand as Record<string, unknown>).series;
			if (ref != null) return String(ref).trim();
		}
		return null;
	};
	const out: Record<string, Array<{ value: number; label: string }>> = {};
	const visit = (item: unknown) => {
		if (!item || typeof item !== 'object') return;
		const node = item as { conditions?: unknown[]; left?: unknown; right?: unknown };
		if (Array.isArray(node.conditions)) {
			node.conditions.forEach(visit);
			return;
		}
		for (const [a, b] of [[node.left, node.right], [node.right, node.left]]) {
			const id = owner.get(seriesName(a) ?? '');
			const found = id ? level(b) : null;
			if (!id || !found || !Number.isFinite(found.value)) continue;
			const lines = (out[id] ??= []);
			if (!lines.some((line) => line.value === found.value)) lines.push(found);
		}
	};
	for (const key of ['entry_long', 'exit_long', 'entry_short', 'exit_short']) visit(spec[key]);
	return out;
}
