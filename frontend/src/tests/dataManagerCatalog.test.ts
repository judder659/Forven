import { describe, expect, it } from 'vitest';
import {
	activeFilterCount,
	applyView,
	BUILTIN_VIEWS,
	EMPTY_FILTERS,
	filtersFromParams,
	filtersToParams,
	filtersToQuery,
	gridTemplate,
	loadColumns,
	loadViews,
	saveColumns,
	saveCustomViews,
	toggleValue,
	viewFromFilters,
	viewMatches,
} from '../lib/components/data-manager/catalogViews';
import { moveIndex, scrollTopFor, windowRange } from '../lib/components/data-manager/windowing';
import { buildCoverage, depthShade, positionOf, rectangle } from '../lib/components/data-manager/coverage';
import { fixtureCatalogRows, fixturePlanDiff } from '../lib/api/dataManagerFixtures';

function memoryStorage(): Storage {
	const data = new Map<string, string>();
	return {
		get length() {
			return data.size;
		},
		clear: () => data.clear(),
		getItem: (key) => data.get(key) ?? null,
		key: (i) => [...data.keys()][i] ?? null,
		removeItem: (key) => void data.delete(key),
		setItem: (key, value) => void data.set(key, String(value)),
	};
}

describe('catalog filters', () => {
	it('round-trips through the URL and leaves defaults out', () => {
		const filters = { ...EMPTY_FILTERS, q: ' btc ', tier: ['live', 'paper'], state: ['late'], sort: 'size' as const, order: 'desc' as const };
		const params = filtersToParams(filters);
		expect(params.toString()).toBe('q=btc&tier=live&tier=paper&state=late&sort=size');
		expect(filtersFromParams(params)).toEqual({ ...filters, q: 'btc' });
		expect(filtersToParams(EMPTY_FILTERS).toString()).toBe('');
	});

	it('reads comma lists and ignores unknown sorts', () => {
		const filters = filtersFromParams(new URLSearchParams('tier=live,paper&sort=nonsense&order=asc'));
		expect(filters.tier).toEqual(['live', 'paper']);
		expect(filters.sort).toBe('priority');
		expect(filters.order).toBe('asc');
		expect(filtersFromParams(new URLSearchParams('sort=symbol')).order).toBe('asc');
	});

	it('becomes a catalog query with paging', () => {
		const query = filtersToQuery({ ...EMPTY_FILTERS, timeframe: ['1m'] }, { limit: 500, offset: 1000 });
		expect(query).toMatchObject({ q: undefined, timeframe: ['1m'], sort: 'priority', order: 'desc', limit: 500, offset: 1000 });
	});

	it('toggles values and counts active filters', () => {
		expect(toggleValue(['a'], 'b')).toEqual(['a', 'b']);
		expect(toggleValue(['a', 'b'], 'a')).toEqual(['b']);
		expect(activeFilterCount({ ...EMPTY_FILTERS, q: 'x', tier: ['live', 'paper'] })).toBe(3);
	});
});

describe('saved views', () => {
	it('ships the five built-in views', () => {
		expect(BUILTIN_VIEWS.map((v) => v.name)).toEqual(['Live & paper', 'Problems', 'Research universe', 'Intraday', 'Everything']);
		expect(viewMatches(BUILTIN_VIEWS[4], EMPTY_FILTERS)).toBe(true);
		expect(viewMatches(BUILTIN_VIEWS[0], applyView(BUILTIN_VIEWS[0]))).toBe(true);
		expect(viewMatches(BUILTIN_VIEWS[0], { ...applyView(BUILTIN_VIEWS[0]), state: ['late'] })).toBe(false);
	});

	it('stores custom views in localStorage and survives junk', () => {
		const store = memoryStorage();
		const mine = viewFromFilters('BTC intraday', { ...EMPTY_FILTERS, q: 'btc', timeframe: ['1m', '5m'], sort: 'size', order: 'desc' });
		expect(mine.filters).toEqual({ q: 'btc', timeframe: ['1m', '5m'], sort: 'size' });
		saveCustomViews([...BUILTIN_VIEWS, mine], store);
		const loaded = loadViews(store);
		expect(loaded).toHaveLength(6);
		expect(loaded[5].name).toBe('BTC intraday');
		expect(applyView(loaded[5])).toEqual({ ...EMPTY_FILTERS, q: 'btc', timeframe: ['1m', '5m'], sort: 'size', order: 'desc' });
		store.setItem('forven.dataManager.views_v1', '{not json');
		expect(loadViews(store)).toHaveLength(5);
	});

	it('remembers the chosen columns', () => {
		const store = memoryStorage();
		expect(loadColumns(store)).toContain('quality');
		saveColumns(['tf', 'freshness', 'bogus' as never], store);
		expect(loadColumns(store)).toEqual(['tf', 'freshness']);
		expect(gridTemplate(['tf', 'size'])).toBe('28px minmax(180px,1.4fr) 52px 76px');
	});
});

describe('windowing', () => {
	it('renders only the rows in view plus overscan', () => {
		expect(windowRange(0, 300, 30, 1700, 5)).toEqual({ start: 0, end: 16, padTop: 0, padBottom: (1700 - 16) * 30 });
		const w = windowRange(30 * 1000, 300, 30, 1700, 5);
		expect(w.start).toBe(995);
		expect(w.end).toBe(1016);
		expect(w.padTop + (w.end - w.start) * 30 + w.padBottom).toBe(1700 * 30);
		expect(windowRange(0, 300, 30, 0)).toEqual({ start: 0, end: 0, padTop: 0, padBottom: 0 });
		const tail = windowRange(1700 * 30, 300, 30, 1700, 5);
		expect(tail.end).toBe(1700);
	});

	it('scrolls just enough to reveal a row', () => {
		expect(scrollTopFor(5, 0, 300, 30)).toBeNull();
		expect(scrollTopFor(20, 0, 300, 30)).toBe(21 * 30 - 300);
		expect(scrollTopFor(2, 300, 300, 30)).toBe(60);
	});

	it('moves with arrows, pages and ends', () => {
		expect(moveIndex(-1, 'ArrowDown', 10, 5)).toBe(0);
		expect(moveIndex(9, 'ArrowDown', 10, 5)).toBe(9);
		expect(moveIndex(0, 'ArrowUp', 10, 5)).toBe(0);
		expect(moveIndex(2, 'PageDown', 10, 5)).toBe(7);
		expect(moveIndex(2, 'End', 10, 5)).toBe(9);
		expect(moveIndex(2, 'Home', 10, 5)).toBe(0);
		expect(moveIndex(2, 'x', 10, 5)).toBeNull();
		expect(moveIndex(0, 'ArrowDown', 0, 5)).toBeNull();
	});
});

describe('coverage grid', () => {
	const rows = fixtureCatalogRows();
	const plan = fixturePlanDiff();

	it('lays out candles by tier with planned-but-missing research cells', () => {
		const model = buildCoverage(rows, plan, { stream: 'ohlcv' });
		expect(model.groups[0].tier).toBe('live');
		expect(model.timeframes.slice(0, 3)).toEqual(['1m', '5m', '15m']);
		const qnt = model.groups.flatMap((g) => g.symbols).find((s) => s.symbol === 'QNT-USDT')!;
		expect(qnt.tier).toBe('universe');
		expect(qnt.cells.filter((c) => c.planned).map((c) => c.timeframe).sort()).toEqual(['15m', '1d', '1h', '1m', '4h', '5m'].sort());
		const btc = model.groups[0].symbols.find((s) => s.symbol === 'BTC-USDT')!;
		expect(btc.cells.find((c) => c.timeframe === '1h')!.row!.sla.tier).toBe('live');
	});

	it('hides frozen series unless asked, and searches', () => {
		const hidden = buildCoverage(rows, null, { stream: 'ohlcv' });
		expect(hidden.flat.flat().some((c) => c.row?.frozen)).toBe(false);
		const shown = buildCoverage(rows, null, { stream: 'ohlcv', showFrozen: true });
		expect(shown.flat.flat().some((c) => c.row?.frozen)).toBe(true);
		const btc = buildCoverage(rows, null, { stream: 'ohlcv', q: 'btc/usdt' });
		expect(btc.groups.flatMap((g) => g.symbols).map((s) => s.symbol)).toEqual(['BTC-USDT']);
	});

	it('selects rectangles and finds cells', () => {
		const model = buildCoverage(rows, plan, { stream: 'ohlcv' });
		const keys = rectangle(model.flat, [0, 0], [1, 2]);
		expect(keys).toHaveLength(6);
		expect(positionOf(model.flat, keys[5])).toEqual([1, 2]);
		expect(positionOf(model.flat, 'nope')).toBeNull();
	});

	it('shades history depth on a log scale', () => {
		expect(depthShade(null, null)).toBe(0);
		expect(depthShade('2019-09-08T00:00:00Z', '2026-09-28T00:00:00Z')).toBeGreaterThan(depthShade('2025-09-28T00:00:00Z', '2026-09-28T00:00:00Z'));
		expect(depthShade('2010-01-01T00:00:00Z', '2026-09-28T00:00:00Z')).toBe(1);
	});
});
