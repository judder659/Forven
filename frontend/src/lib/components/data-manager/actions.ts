/**
 * Running a Data Manager action (refresh, repair, cancel, reclaim, ...) with one
 * consistent outcome: a toast that says what happened, the jobs poller kicked
 * into fast mode, and a plain message when the backend route is not there yet.
 */
import { isRouteMissingError } from '$lib/api/core';
import { downloadDataset } from '$lib/api/data';
import type { SeriesKey } from '$lib/api/dataManagerTypes';
import { addToast } from '$lib/stores/processTracker';
import { errorMessage, pokeJobs } from '$lib/stores/dataManager';

export async function runAction<T>(
	label: string,
	action: () => Promise<T>,
	options: { success?: (result: T) => string; href?: string; poke?: boolean } = {},
): Promise<T | null> {
	try {
		const result = await action();
		if (options.success) addToast(options.success(result), 'success', options.href);
		if (options.poke !== false) pokeJobs();
		return result;
	} catch (error) {
		addToast(
			isRouteMissingError(error)
				? `${label} is not available yet: this backend does not serve it.`
				: `${label} failed: ${errorMessage(error)}`,
			'error',
		);
		return null;
	}
}

export function keyOf(row: { symbol: string; timeframe: string; stream: SeriesKey['stream']; venue: string }): SeriesKey {
	return { symbol: row.symbol, timeframe: row.timeframe, stream: row.stream, venue: row.venue };
}

/** Series the old export route can write: canonical candles. */
export const canExport = (row: { stream: string; venue: string }) => row.stream === 'ohlcv' && row.venue === 'canonical';

/** Deep history comes from Binance Vision: canonical candles and perp streams. */
export const canExtend = (row: { stream: string; venue: string }) =>
	row.venue === 'canonical' && ['ohlcv', 'funding', 'oi', 'basis'].includes(row.stream);

/** Save a canonical candle series as CSV through the existing export route. */
export async function exportSeriesCsv(row: { display_symbol: string; symbol: string; timeframe: string }): Promise<void> {
	const blob = await downloadDataset(row.display_symbol, row.timeframe, 'csv');
	const url = URL.createObjectURL(blob);
	const link = document.createElement('a');
	link.href = url;
	link.download = `${row.symbol}_${row.timeframe}.csv`;
	document.body.appendChild(link);
	link.click();
	link.remove();
	setTimeout(() => URL.revokeObjectURL(url), 1_000);
}

/** Save any blob under a file name. */
export function saveBlob(blob: Blob, filename: string): void {
	const url = URL.createObjectURL(blob);
	const link = document.createElement('a');
	link.href = url;
	link.download = filename;
	document.body.appendChild(link);
	link.click();
	link.remove();
	setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
