// Helpers for the Strategy Creator's parameter heatmap and market grid: which
// numbers a rule is tuned by, the values an axis sweeps, what a grid says about
// robustness, and which markets have local data.
import type { IndicatorMeta, MarketRow, SampleStats } from '$lib/api';

type Spec = Record<string, unknown>;

/** A number a rule is tuned by: one of its params ("knobs") or an indicator setting. */
export interface KnobOption {
	key: string;
	target: 'param' | 'indicator';
	name: string;
	indicator: string | null;
	label: string;
	value: number;
	integer: boolean;
	min: number | null;
	max: number | null;
}

export function hashSpec(spec: unknown): string {
	const s = JSON.stringify(spec ?? {});
	let h = 5381;
	for (let i = 0; i < s.length; i++) h = ((h * 33) ^ s.charCodeAt(i)) >>> 0;
	return h.toString(36);
}

/** Identity of one result seen: these rules on this market. */
export function resultKey(spec: unknown, symbol: string, timeframe: string): string {
	return `${hashSpec(spec)}|${symbol.trim().toUpperCase()}|${timeframe.trim()}`;
}

function clone<T>(value: T): T {
	return JSON.parse(JSON.stringify(value));
}

export function specKnobs(spec: Spec | null, metaByKind: Record<string, IndicatorMeta>): KnobOption[] {
	if (!spec) return [];
	const knobs: KnobOption[] = [];
	for (const [name, value] of Object.entries((spec.params ?? {}) as Record<string, unknown>)) {
		if (typeof value !== 'number' || !Number.isFinite(value)) continue;
		knobs.push({ key: `param:${name}`, target: 'param', name, indicator: null, label: name, value,
			integer: false, min: null, max: null });
	}
	for (const instance of (spec.indicators ?? []) as Array<{ id: string; kind: string; params?: Record<string, number> }>) {
		const meta = metaByKind[instance.kind];
		for (const param of meta?.params ?? []) {
			const value = Number(instance.params?.[param.key] ?? param.default);
			if (!Number.isFinite(value)) continue;
			knobs.push({ key: `ind:${instance.id}:${param.key}`, target: 'indicator', name: param.key, indicator: instance.id,
				label: `${instance.id} ${param.key}`, value, integer: param.step >= 1 && Number.isInteger(param.default),
				min: param.min, max: param.max });
		}
	}
	return knobs;
}

type KnobRef = Pick<KnobOption, 'target' | 'name' | 'indicator'>;

export function knobValue(spec: Spec | null, knob: KnobRef): number | null {
	if (!spec) return null;
	if (knob.target === 'param') {
		const value = (spec.params as Record<string, unknown> | undefined)?.[knob.name];
		return typeof value === 'number' ? value : null;
	}
	const instance = ((spec.indicators ?? []) as Array<{ id: string; params?: Record<string, number> }>)
		.find((ind) => ind.id === knob.indicator);
	const value = instance?.params?.[knob.name];
	return typeof value === 'number' ? value : null;
}

/** A copy of the spec with some settings changed; everything else, and key order, kept. */
export function withKnobs(spec: Spec, changes: Array<[KnobRef, number | null]>): Spec {
	const next = clone(spec);
	for (const [knob, value] of changes) {
		if (value === null) continue;
		if (knob.target === 'param') {
			next.params = { ...((next.params ?? {}) as Record<string, number>), [knob.name]: value };
			continue;
		}
		for (const instance of (next.indicators ?? []) as Array<{ id: string; params?: Record<string, number> }>) {
			if (instance.id === knob.indicator) instance.params = { ...(instance.params ?? {}), [knob.name]: value };
		}
	}
	return next;
}

/** The spec with the swept settings blanked, so moving along an axis keeps the grid current. */
export function withoutKnobs(spec: Spec | null, knobs: KnobRef[]): Spec | null {
	if (!spec) return null;
	return withKnobs(spec, knobs.map((knob) => [knob, Number.NaN])) as Spec;
}

/** Default sweep around a value: half to one and a half times it. */
export function defaultRange(value: number): { from: number; to: number } {
	if (value === 0) return { from: -1, to: 1 };
	const a = value * 0.5;
	const b = value * 1.5;
	return { from: Math.min(a, b), to: Math.max(a, b) };
}

/** The round step (1, 2, 2.5 or 5 times a power of ten) nearest to `raw`. */
function niceStep(raw: number): number {
	if (!(raw > 0)) return 1;
	const power = 10 ** Math.floor(Math.log10(raw));
	const candidates = [1, 2, 2.5, 5, 10].map((unit) => unit * power);
	return candidates.reduce((best, c) => (Math.abs(Math.log(c / raw)) < Math.abs(Math.log(best / raw)) ? c : best));
}

/** A default sweep of `steps` values centred on the current value, in round steps
 * spanning about half to one and a half times it. */
export function niceRange(value: number, steps: number, integer: boolean): { from: number; to: number } {
	const { from, to } = defaultRange(value);
	const count = Math.max(2, Math.round(steps));
	let step = niceStep((to - from) / (count - 1));
	if (integer) step = Math.max(1, Math.round(step));
	const below = Math.floor((count - 1) / 2);
	return { from: +(value - step * below).toFixed(6), to: +(value + step * (count - 1 - below)).toFixed(6) };
}

/** Evenly spaced values from `from` to `to`, rounded like the setting, within its bounds, without repeats. */
export function axisValues(from: number, to: number, steps: number, knob: Pick<KnobOption, 'integer' | 'min' | 'max'>): number[] {
	const count = Math.max(1, Math.min(9, Math.round(steps)));
	const values: number[] = [];
	for (let i = 0; i < count; i++) {
		const raw = count === 1 ? from : from + ((to - from) * i) / (count - 1);
		let value = knob.integer ? Math.round(raw) : Math.round(raw * 1e6) / 1e6;
		if (knob.min !== null && value < knob.min) value = knob.min;
		if (knob.max !== null && value > knob.max) value = knob.max;
		if (!values.includes(value)) values.push(value);
	}
	return values;
}

// ---- Verdicts --------------------------------------------------------------------
export interface HeatmapCellLike {
	x: number;
	y: number | null;
	oos_return?: number;
	error?: string;
}

export interface Verdict {
	status: string;
	text: string;
}

/** Whether the best cell sits on a plateau (neighbours keep most of its result) or a lone spike. */
export function heatmapVerdict(cells: HeatmapCellLike[], xValues: number[], yValues: Array<number | null>): Verdict | null {
	const at = new Map(cells.filter((c) => c.oos_return !== undefined && !c.error).map((c) => [`${c.x}|${c.y}`, c]));
	if (!at.size) return null;
	const scored = [...at.values()];
	const best = scored.reduce((a, b) => ((b.oos_return ?? -Infinity) > (a.oos_return ?? -Infinity) ? b : a));
	const bestReturn = best.oos_return ?? 0;
	const profitable = scored.filter((c) => (c.oos_return ?? 0) > 0).length;
	const share = `${profitable} of ${scored.length} setting${scored.length === 1 ? '' : 's'} ${profitable === 1 ? 'makes' : 'make'} money out-of-sample.`;
	if (bestReturn <= 0) return { status: 'losing', text: `No setting in this grid makes money out-of-sample.` };
	const xi = xValues.indexOf(best.x);
	const yi = yValues.indexOf(best.y);
	const neighbours: HeatmapCellLike[] = [];
	for (let dy = -1; dy <= 1; dy++) {
		for (let dx = -1; dx <= 1; dx++) {
			if (!dx && !dy) continue;
			const x = xValues[xi + dx];
			const y = yValues[yi + dy];
			const cell = x === undefined || y === undefined ? undefined : at.get(`${x}|${y}`);
			if (cell) neighbours.push(cell);
		}
	}
	if (!neighbours.length) return { status: 'unknown', text: share };
	const holding = neighbours.filter((c) => (c.oos_return ?? 0) >= 0.5 * bestReturn).length;
	const ratio = holding / neighbours.length;
	const counted = `${holding} of ${neighbours.length} neighbours of the best setting keep at least half its out-of-sample return`;
	if (ratio >= 0.6) return { status: 'plateau', text: `Plateau: ${counted}, so the edge does not hinge on one exact setting. ${share}` };
	if (ratio <= 0.25) return { status: 'spike', text: `Spike: only ${counted}. A lone bright cell is usually fitted noise. ${share}` };
	return { status: 'uneven', text: `Uneven: ${counted}. ${share}` };
}

const MIN_SCORED_TRADES = 5;

/** Whether an edge carries across markets. */
export function marketVerdict(rows: MarketRow[]): Verdict | null {
	const ok = rows.filter((row) => row.status === 'ok' && row.out_of_sample);
	if (!ok.length) return null;
	const scored = ok.filter((row) => (row.out_of_sample as SampleStats).trades >= MIN_SCORED_TRADES);
	const thin = ok.length - scored.length;
	const thinNote = thin ? ` ${thin} market${thin === 1 ? '' : 's'} had fewer than ${MIN_SCORED_TRADES} out-of-sample trades and ${thin === 1 ? 'is' : 'are'} left out.` : '';
	if (scored.length < 2) return { status: 'unknown', text: `Too few markets with enough out-of-sample trades to judge.${thinNote}` };
	const returns = scored.map((row) => (row.out_of_sample as SampleStats).net_return).sort((a, b) => a - b);
	const mid = Math.floor(returns.length / 2);
	const median = returns.length % 2 ? returns[mid] : (returns[mid - 1] + returns[mid]) / 2;
	const positive = returns.filter((value) => value > 0).length;
	const share = positive / scored.length;
	const counted = `${positive} of ${scored.length} markets`;
	const medianText = `median ${median > 0 ? '+' : ''}${(median * 100).toFixed(1)}%`;
	if (!positive) return { status: 'none', text: `No market is profitable out-of-sample (${medianText}).${thinNote}` };
	if (share >= 0.6 && median > 0) {
		return { status: 'broad', text: `Profitable out-of-sample on ${counted} (${medianText}): the edge carries beyond one market.${thinNote}` };
	}
	if (share < 0.5 && median <= 0) {
		return { status: 'none', text: `Loses out-of-sample on most markets: ${counted} ${positive === 1 ? 'is' : 'are'} profitable (${medianText}).${thinNote}` };
	}
	return { status: 'narrow', text: `Profitable out-of-sample on only ${counted} (${medianText}): the edge may belong to the market it was built on.${thinNote}` };
}

// ---- Markets ---------------------------------------------------------------------
/** The base asset a backtest reads, as the API resolves it ("BTC/USDT" → "BTC"). */
export function baseAsset(symbol: string): string {
	let raw = symbol.trim().toUpperCase();
	if (!raw) return 'BTC';
	for (const sep of ['/', '-', '_']) {
		if (raw.includes(sep)) {
			raw = raw.split(sep)[0];
			break;
		}
	}
	for (const suffix of ['PERP', 'USDT', 'USDC', 'USD']) {
		if (raw.endsWith(suffix) && raw.length > suffix.length) {
			raw = raw.slice(0, -suffix.length);
			break;
		}
	}
	return raw.trim() || 'BTC';
}

const BACKTEST_QUOTES = ['', 'USDT', 'USD', 'USDC'];

/** Timeframes with a local dataset, by base asset. The backtest loader reads a
 * base asset's bare, /USDT, /USD or /USDC series, so "ADA/BTC" does not count. */
export function marketAvailability(datasets: Array<{ symbol: string; timeframe: string }>): Map<string, Set<string>> {
	const byBase = new Map<string, Set<string>>();
	for (const dataset of datasets) {
		const [base, quote = ''] = String(dataset.symbol || '').toUpperCase().split('/');
		if (!base || !BACKTEST_QUOTES.includes(quote)) continue;
		if (!byBase.has(base)) byBase.set(base, new Set());
		byBase.get(base)!.add(String(dataset.timeframe));
	}
	return byBase;
}

export function hasLocalData(availability: Map<string, Set<string>>, symbol: string, timeframe: string): boolean {
	return availability.get(baseAsset(symbol))?.has(timeframe) ?? false;
}

/** Split `items` into at most `parts` contiguous groups of near-equal size. Each
 * group is one request: a fresh backtest worker spends seconds on first-use
 * imports, so a few larger requests beat many small ones. */
export function chunk<T>(items: T[], parts: number): T[][] {
	const count = Math.max(1, Math.min(parts, items.length));
	const groups: T[][] = [];
	let start = 0;
	for (let i = 0; i < count; i++) {
		const size = Math.ceil((items.length - start) / (count - i));
		groups.push(items.slice(start, start + size));
		start += size;
	}
	return groups.filter((group) => group.length);
}

/** Run `work` over `items` with at most `limit` in flight; stops taking new items once aborted. */
export async function runPool<T>(items: T[], limit: number, work: (item: T) => Promise<void>, signal: AbortSignal): Promise<void> {
	let next = 0;
	const lane = async () => {
		while (!signal.aborted && next < items.length) {
			const item = items[next++];
			await work(item);
		}
	};
	await Promise.all(Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, lane));
}
