/**
 * Data handling behind the series chart: wire bars to chart rows, and when the
 * visible range needs data the chart does not have (finer bars after a zoom
 * in, or bars beyond the loaded window after a pan).
 */
import type { Bar, GapSpan } from '$lib/api/dataManagerTypes';

export interface ChartBar {
	time: number;
	open: number;
	high: number;
	low: number;
	close: number;
}

export const toSeconds = (iso: string) => Math.floor(Date.parse(iso) / 1000);

export function toChartBars(bars: Bar[]): ChartBar[] {
	const rows = bars
		.map((bar) => ({ time: toSeconds(bar.t), open: bar.o, high: bar.h, low: bar.l, close: bar.c }))
		.filter((row) => Number.isFinite(row.time) && Number.isFinite(row.close));
	rows.sort((a, b) => a.time - b.time);
	// The chart needs strictly increasing times.
	return rows.filter((row, i) => i === 0 || row.time > rows[i - 1].time);
}

/** Index of the last time <= t (or -1). */
export function floorIndex(times: number[], t: number): number {
	let lo = 0;
	let hi = times.length - 1;
	let found = -1;
	while (lo <= hi) {
		const mid = (lo + hi) >> 1;
		if (times[mid] <= t) {
			found = mid;
			lo = mid + 1;
		} else hi = mid - 1;
	}
	return found;
}

export function countInRange(times: number[], from: number, to: number): number {
	if (!times.length || to < from) return 0;
	const last = floorIndex(times, to);
	const before = floorIndex(times, from - 1);
	return Math.max(0, last - before);
}

export interface LoadedWindow {
	/** First and last loaded bar time (seconds). */
	start: number;
	end: number;
	raw: boolean;
	times: number[];
}

/** The window to fetch for a visible range, or null when the loaded data serves it.
 * Fetches finer bars once fewer than `minBars` downsampled bars are visible, and
 * more bars when the view runs past the loaded window inside the series span. */
export function windowToFetch(
	visible: { from: number; to: number },
	loaded: LoadedWindow,
	series: { first: number; last: number; step: number },
	minBars = 80,
): { start: number; end: number } | null {
	const span = Math.max(series.step, visible.to - visible.from);
	const beyondStart = visible.from < loaded.start - series.step && loaded.start > series.first + series.step;
	const beyondEnd = visible.to > loaded.end + series.step && loaded.end < series.last - series.step;
	// Only a zoom into part of the loaded window can get finer bars: refetching
	// the window already in view would return the same buckets again.
	const zoomedIn = visible.to - visible.from < (loaded.end - loaded.start) * 0.9;
	const coarse = !loaded.raw && zoomedIn && countInRange(loaded.times, visible.from, visible.to) < minBars;
	if (!beyondStart && !beyondEnd && !coarse) return null;
	return {
		start: Math.max(series.first, Math.floor(visible.from - span / 2)),
		end: Math.min(series.last, Math.ceil(visible.to + span / 2)),
	};
}

/** Gap markers on the bar before each gap (largest gaps first, capped). */
export function gapMarkers(gaps: GapSpan[], times: number[], max = 40): Array<{ time: number; bars: number; kind: GapSpan['kind'] }> {
	const seen = new Set<number>();
	const markers: Array<{ time: number; bars: number; kind: GapSpan['kind'] }> = [];
	for (const gap of [...gaps].sort((a, b) => b.bars - a.bars)) {
		if (markers.length >= max) break;
		const index = floorIndex(times, toSeconds(gap.start) - 1);
		if (index < 0 || seen.has(times[index])) continue;
		seen.add(times[index]);
		markers.push({ time: times[index], bars: gap.bars, kind: gap.kind });
	}
	return markers.sort((a, b) => a.time - b.time);
}
