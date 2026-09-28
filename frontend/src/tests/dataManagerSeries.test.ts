import { describe, expect, it } from 'vitest';
import { countInRange, floorIndex, gapMarkers, toChartBars, toSeconds, windowToFetch } from '../lib/components/data-manager/seriesChart';
import { candidateHref, catalogHref, seriesHref } from '../lib/components/data-manager/links';
import { timeframeSeconds } from '../lib/components/data-manager/format';
import { fixtureBars, fixtureSeriesDetail } from '../lib/api/dataManagerFixtures';

const H = 3600;

describe('series chart data', () => {
	it('maps wire bars to sorted, strictly increasing chart rows', () => {
		const rows = toChartBars([
			{ t: '2026-01-01T02:00:00Z', o: 3, h: 4, l: 2, c: 3.5, v: 1 },
			{ t: '2026-01-01T01:00:00Z', o: 1, h: 2, l: 0.5, c: 1.5, v: 1 },
			{ t: '2026-01-01T01:00:00Z', o: 9, h: 9, l: 9, c: 9, v: 1 },
			{ t: 'garbage', o: 1, h: 1, l: 1, c: 1, v: 1 },
		]);
		expect(rows.map((r) => r.time)).toEqual([toSeconds('2026-01-01T01:00:00Z'), toSeconds('2026-01-01T02:00:00Z')]);
		expect(rows[1]).toMatchObject({ open: 3, high: 4, low: 2, close: 3.5 });
	});

	it('counts bars in a range by binary search', () => {
		const times = [10, 20, 30, 40, 50];
		expect(floorIndex(times, 5)).toBe(-1);
		expect(floorIndex(times, 30)).toBe(2);
		expect(floorIndex(times, 35)).toBe(2);
		expect(countInRange(times, 20, 40)).toBe(3);
		expect(countInRange(times, 41, 49)).toBe(0);
	});

	it('fetches finer bars after a zoom in, and more bars after a pan past the loaded window', () => {
		const series = { first: 0, last: 1000 * H, step: H };
		const daily = Array.from({ length: 42 }, (_, i) => i * 24 * H);
		const full = { start: 0, end: 41 * 24 * H, raw: false, times: daily };
		// Whole history visible: the downsampled bars serve it.
		expect(windowToFetch({ from: 0, to: 41 * 24 * H }, full, series)).toBeNull();
		// Zoomed to 3 days: only 3-4 daily bars visible, fetch that window with padding.
		const zoom = windowToFetch({ from: 10 * 24 * H, to: 13 * 24 * H }, full, series);
		expect(zoom).toEqual({ start: 10 * 24 * H - 36 * H, end: 13 * 24 * H + 36 * H });
		// Raw window loaded for days 10-13; panning to day 20 needs more bars.
		const raw = { start: 10 * 24 * H, end: 13 * 24 * H, raw: true, times: Array.from({ length: 73 }, (_, i) => 10 * 24 * H + i * H) };
		expect(windowToFetch({ from: 10 * 24 * H, to: 12 * 24 * H }, raw, series)).toBeNull();
		expect(windowToFetch({ from: 19 * 24 * H, to: 21 * 24 * H }, raw, series)).not.toBeNull();
		// Never past the series span.
		expect(windowToFetch({ from: 990 * H, to: 1010 * H }, { ...raw, start: 900 * H, end: 950 * H }, series)?.end).toBe(1000 * H);
	});

	it('marks the bar before each gap, largest gaps first', () => {
		const detail = fixtureSeriesDetail({ symbol: 'BTC-USDT', timeframe: '1h' })!;
		const bars = fixtureBars({ symbol: 'BTC-USDT', timeframe: '1h' }, { start: '2020-08-01T00:00:00Z', end: '2020-08-31T23:00:00Z', max_points: 3000 })!;
		expect(bars.raw).toBe(true);
		const times = toChartBars(bars.bars).map((b) => b.time);
		const markers = gapMarkers(detail.gaps, times);
		expect(markers).toHaveLength(detail.gaps.length);
		const gap = detail.gaps[0];
		expect(markers[0].time).toBe(toSeconds(gap.start) - H);
		expect(markers[0].bars).toBe(gap.bars);
		// The gap's own bars are absent from the raw window.
		expect(times.includes(toSeconds(gap.start))).toBe(false);
	});
});

describe('links', () => {
	it('addresses series by file-system symbol with non-default stream and venue only', () => {
		expect(seriesHref({ symbol: 'BTC-USDT', timeframe: '1h' })).toBe('/data-next/series/BTC-USDT/1h');
		expect(seriesHref({ symbol: 'BTC-USDT', timeframe: '4h', stream: 'ohlcv', venue: 'hyperliquid:perp' })).toBe('/data-next/series/BTC-USDT/4h?venue=hyperliquid%3Aperp');
		expect(seriesHref({ symbol: 'ETH-USDT', timeframe: '8h', stream: 'funding', venue: 'canonical' })).toBe('/data-next/series/ETH-USDT/8h?stream=funding');
		expect(catalogHref({ tier: ['live', 'paper'], q: 'btc' })).toBe('/data-next/catalog?tier=live&tier=paper&q=btc');
	});

	it('sends a search result to its best stored series, or to Get data', () => {
		const stored = (timeframe: string, venue = 'canonical', stream = 'ohlcv' as const) => ({ timeframe, venue, stream, rows: 10, last_ts: null });
		expect(candidateHref({ symbol: 'BTC-USDT', stored: [stored('5m'), stored('1h'), stored('1h', 'hyperliquid:perp')] })).toBe('/data-next/series/BTC-USDT/1h');
		expect(candidateHref({ symbol: 'ETH-USDT', stored: [stored('4h', 'okx:perp')] })).toBe('/data-next/series/ETH-USDT/4h?venue=okx%3Aperp');
		expect(candidateHref({ symbol: 'QNT-USDT', stored: [] })).toBe('/data-next/get?symbol=QNT-USDT');
	});

	it('knows bar widths', () => {
		expect(timeframeSeconds('15m')).toBe(900);
		expect(timeframeSeconds('4h')).toBe(4 * H);
		expect(timeframeSeconds('1w')).toBe(7 * 24 * H);
		expect(timeframeSeconds('?')).toBe(H);
	});
});
