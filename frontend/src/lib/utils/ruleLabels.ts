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
