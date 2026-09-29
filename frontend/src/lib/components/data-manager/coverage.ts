/**
 * The Coverage grid: symbols × timeframes for one stream, grouped by the most
 * important tier any of the symbol's series has. Cell colour is the server's
 * SLA state; planned-but-missing research cells come from the plan diff.
 */
import type { CatalogRow, DataStream, SlaTier, UniversePlanDiff } from '$lib/api/dataManagerTypes';
import { TIERS } from './format';

const TF_ORDER = ['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '8h', '12h', '1d', '3d', '1w'];
export function compareTimeframes(a: string, b: string): number {
	const ia = TF_ORDER.indexOf(a);
	const ib = TF_ORDER.indexOf(b);
	return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b);
}

export interface CoverageCell {
	key: string;
	symbol: string;
	timeframe: string;
	row: CatalogRow | null;
	/** In the research plan but not stored. */
	planned: boolean;
}

export interface CoverageSymbol {
	symbol: string;
	display: string;
	tier: SlaTier;
	cells: CoverageCell[];
}

export interface CoverageGroup {
	tier: SlaTier;
	symbols: CoverageSymbol[];
}

export interface CoverageModel {
	timeframes: string[];
	groups: CoverageGroup[];
	/** Row-major list of every rendered cell, for keyboard navigation and range selection. */
	flat: CoverageCell[][];
}

export const cellKey = (symbol: string, timeframe: string) => `${symbol}|${timeframe}`;

export function buildCoverage(
	rows: CatalogRow[],
	plan: UniversePlanDiff | null,
	options: { stream: DataStream; venue?: string; q?: string; showFrozen?: boolean },
): CoverageModel {
	const venue = options.venue ?? 'canonical';
	const q = (options.q ?? '').trim().toUpperCase().replace(/[/_ ]/g, '-');
	const matches = (symbol: string) => !q || symbol.includes(q) || symbol.replace(/-/g, '').includes(q.replace(/-/g, ''));
	const bySymbol = new Map<string, Map<string, CatalogRow>>();
	const tfs = new Set<string>();
	for (const row of rows) {
		if (row.stream !== options.stream || row.venue !== venue || !matches(row.symbol)) continue;
		if (row.frozen && !options.showFrozen) continue;
		tfs.add(row.timeframe);
		let cells = bySymbol.get(row.symbol);
		if (!cells) bySymbol.set(row.symbol, (cells = new Map()));
		cells.set(row.timeframe, row);
	}
	const planned = new Map<string, Set<string>>();
	if (plan && options.stream === 'ohlcv' && venue === 'canonical') {
		for (const item of plan.missing) {
			if (!matches(item.symbol)) continue;
			planned.set(item.symbol, new Set(item.timeframes));
			for (const tf of item.timeframes) tfs.add(tf);
			if (!bySymbol.has(item.symbol)) bySymbol.set(item.symbol, new Map());
		}
	}
	const timeframes = [...tfs].sort(compareTimeframes);
	const symbols: CoverageSymbol[] = [...bySymbol.entries()].map(([symbol, cells]) => {
		const stored = [...cells.values()];
		const tier = TIERS.find((t) => stored.some((row) => row.sla.tier === t)) ?? (planned.has(symbol) ? 'universe' : 'idle');
		return {
			symbol,
			display: stored[0]?.display_symbol ?? symbol.replace('-', '/'),
			tier,
			cells: timeframes.map((timeframe) => ({
				key: cellKey(symbol, timeframe),
				symbol,
				timeframe,
				row: cells.get(timeframe) ?? null,
				planned: !cells.has(timeframe) && !!planned.get(symbol)?.has(timeframe),
			})),
		};
	});
	const groups: CoverageGroup[] = TIERS.map((tier) => ({
		tier,
		symbols: symbols.filter((s) => s.tier === tier).sort((a, b) => a.symbol.localeCompare(b.symbol)),
	})).filter((g) => g.symbols.length > 0);
	return { timeframes, groups, flat: groups.flatMap((g) => g.symbols.map((s) => s.cells)) };
}

/** Cells in the rectangle between two grid positions (inclusive). */
export function rectangle(flat: CoverageCell[][], from: [number, number], to: [number, number]): string[] {
	const [r0, r1] = [Math.min(from[0], to[0]), Math.max(from[0], to[0])];
	const [c0, c1] = [Math.min(from[1], to[1]), Math.max(from[1], to[1])];
	const keys: string[] = [];
	for (let r = r0; r <= r1; r++) for (let c = c0; c <= c1; c++) if (flat[r]?.[c]) keys.push(flat[r][c].key);
	return keys;
}

/** Position of a cell in the flat grid. */
export function positionOf(flat: CoverageCell[][], key: string): [number, number] | null {
	for (let r = 0; r < flat.length; r++) {
		const c = flat[r].findIndex((cell) => cell.key === key);
		if (c >= 0) return [r, c];
	}
	return null;
}

/** History depth as a 0..1 shade (log scale, a decade of history = full). A
 * measurement for the depth view, not a health threshold. */
export function depthShade(first: string | null, last: string | null): number {
	if (!first || !last) return 0;
	const years = (Date.parse(last) - Date.parse(first)) / (365.25 * 86_400_000);
	if (!(years > 0)) return 0.05;
	return Math.max(0.08, Math.min(1, Math.log10(1 + years * 9) / Math.log10(91)));
}
