// Running a heatmap or market grid as a few requests at a time: the work is split
// into at most three near-equal requests (a fresh backtest worker spends seconds
// on first-use imports, so a few larger requests beat many small ones), results
// land as each request finishes, and an aborted run stops quietly.
import type { HeatmapAxisRequest, HeatmapCell, HeatmapResult, MarketRow, MarketsResult } from '$lib/api';
import { chunk, runPool } from './creatorGrids';

const PARALLEL = 3;

export interface Market {
	symbol: string;
	timeframe: string;
}

export async function runHeatmapRequests(
	axes: { x: HeatmapAxisRequest; y: HeatmapAxisRequest | null },
	fetchPart: (x: HeatmapAxisRequest, y: HeatmapAxisRequest | null, signal: AbortSignal) => Promise<HeatmapResult>,
	handlers: {
		/** A request's cells; `size` is how many cells it covered. */
		cells(cells: HeatmapCell[], warnings: string[], size: number): void;
		failed(message: string, size: number): void;
	},
	signal: AbortSignal,
): Promise<void> {
	// Rows (or, with one axis, values) are split so each request covers whole rows.
	const parts = axes.y
		? chunk(axes.y.values, PARALLEL).map((ys) => ({ xs: axes.x.values, ys: ys as number[] | null }))
		: chunk(axes.x.values, PARALLEL).map((xs) => ({ xs, ys: null as number[] | null }));
	await runPool(parts, PARALLEL, async ({ xs, ys }) => {
		const size = xs.length * (ys ? ys.length : 1);
		try {
			const result = await fetchPart({ ...axes.x, values: xs }, axes.y && ys ? { ...axes.y, values: ys } : null, signal);
			if (!signal.aborted) handlers.cells(result.cells, result.warnings, size);
		} catch (err) {
			if (!signal.aborted) handlers.failed(err instanceof Error ? err.message : 'A heatmap request failed.', size);
		}
	}, signal);
}

export async function runMarketRequests(
	markets: Market[],
	fetchPart: (markets: Market[], signal: AbortSignal) => Promise<MarketsResult>,
	handlers: {
		/** One row per requested market, in order. */
		rows(markets: Market[], rows: MarketRow[], warnings: string[]): void;
		failed(markets: Market[], message: string): void;
	},
	signal: AbortSignal,
): Promise<void> {
	await runPool(chunk(markets, PARALLEL), PARALLEL, async (group) => {
		try {
			const result = await fetchPart(group, signal);
			if (signal.aborted) return;
			const rows = group.map((market, i): MarketRow =>
				result.rows[i] ?? { ...market, status: 'error', message: result.warnings[0] ?? 'No result.' });
			handlers.rows(group, rows, result.warnings);
		} catch (err) {
			if (!signal.aborted) handlers.failed(group, err instanceof Error ? err.message : 'These markets failed.');
		}
	}, signal);
}
