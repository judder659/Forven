import { describe, expect, it } from 'vitest';
import type { IndicatorMeta, MarketRow } from '$lib/api';
import {
	axisValues,
	baseAsset,
	chunk,
	defaultRange,
	hasLocalData,
	heatmapVerdict,
	niceRange,
	knobValue,
	marketAvailability,
	marketVerdict,
	resultKey,
	runPool,
	specKnobs,
	withKnobs,
	withoutKnobs,
} from '$lib/utils/creatorGrids';

const RSI_META = {
	kind: 'rsi', label: 'RSI', category: 'Momentum', description: '', panel: 'sub', multi_output: false,
	params: [{ key: 'length', type: 'number', default: 14, min: 2, max: 100, step: 1 }], output_suffixes: [''],
} as IndicatorMeta;
const SPEC = {
	indicators: [{ id: 'rsi', kind: 'rsi', params: { length: 14 } }],
	params: { oversold: 30, exit_level: 55 },
	entry_long: { logic: 'and', conditions: [{ left: 'rsi', op: '<', right: { param: 'oversold' } }] },
	exit_long: null, entry_short: null, exit_short: null,
};
const oversold = { target: 'param' as const, name: 'oversold', indicator: null };
const length = { target: 'indicator' as const, name: 'length', indicator: 'rsi' };

describe('knobs', () => {
	it('lists params, then indicator settings with their bounds', () => {
		const knobs = specKnobs(SPEC, { rsi: RSI_META });
		expect(knobs.map((k) => [k.label, k.value, k.integer, k.min])).toEqual([
			['oversold', 30, false, null], ['exit_level', 55, false, null], ['rsi length', 14, true, 2],
		]);
	});

	it('changes settings on a copy, keeping key order so the result hashes like the builder output', () => {
		const changed = withKnobs(SPEC, [[oversold, 25], [length, 21]]);
		expect(knobValue(changed, oversold)).toBe(25);
		expect(knobValue(changed, length)).toBe(21);
		expect(SPEC.params.oversold).toBe(30);
		expect(JSON.stringify(withKnobs(changed, [[oversold, 30], [length, 14]]))).toBe(JSON.stringify(SPEC));
		expect(JSON.stringify(withoutKnobs(SPEC, [oversold]))).toContain('"oversold":null');
	});

	it('keys a result by the rules and the market', () => {
		expect(resultKey(SPEC, 'btc/usdt ', '1h')).toBe(resultKey(SPEC, 'BTC/USDT', '1h'));
		expect(resultKey(SPEC, 'BTC/USDT', '4h')).not.toBe(resultKey(SPEC, 'BTC/USDT', '1h'));
		expect(resultKey(withKnobs(SPEC, [[oversold, 25]]), 'BTC/USDT', '1h')).not.toBe(resultKey(SPEC, 'BTC/USDT', '1h'));
	});
});

describe('axis values', () => {
	it('sweeps half to one and a half times the current value, including it', () => {
		expect(defaultRange(30)).toEqual({ from: 15, to: 45 });
		expect(defaultRange(-2)).toEqual({ from: -3, to: -1 });
		expect(defaultRange(0)).toEqual({ from: -1, to: 1 });
		expect(axisValues(15, 45, 7, { integer: false, min: null, max: null })).toEqual([15, 20, 25, 30, 35, 40, 45]);
	});

	it('centres default sweeps on the current value in round steps', () => {
		expect(niceRange(30, 7, false)).toEqual({ from: 15, to: 45 });
		expect(niceRange(55, 7, false)).toEqual({ from: 25, to: 85 });
		expect(niceRange(55, 3, false)).toEqual({ from: 30, to: 80 });
		expect(niceRange(14, 7, true)).toEqual({ from: 5, to: 23 });
		expect(niceRange(-2, 5, false)).toEqual({ from: -3, to: -1 });
		expect(axisValues(25, 85, 7, { integer: false, min: null, max: null })).toEqual([25, 35, 45, 55, 65, 75, 85]);
	});

	it('rounds whole-number settings, respects their bounds and drops repeats', () => {
		expect(axisValues(1, 21, 7, { integer: true, min: 2, max: null })).toEqual([2, 4, 8, 11, 14, 18, 21]);
		expect(axisValues(1, 3, 9, { integer: true, min: 2, max: 3 })).toEqual([2, 3]);
		expect(axisValues(0.1, 0.2, 3, { integer: false, min: null, max: null })).toEqual([0.1, 0.15, 0.2]);
	});
});

describe('heatmap verdict', () => {
	const grid = (fn: (x: number, y: number) => number) =>
		[1, 2, 3].flatMap((y) => [1, 2, 3].map((x) => ({ x, y, oos_return: fn(x, y) })));

	it('calls a best setting with strong neighbours a plateau', () => {
		expect(heatmapVerdict(grid((x, y) => (x === 2 && y === 2 ? 0.1 : 0.08)), [1, 2, 3], [1, 2, 3])?.status).toBe('plateau');
	});
	it('calls a lone bright cell a spike', () => {
		const verdict = heatmapVerdict(grid((x, y) => (x === 2 && y === 2 ? 0.2 : -0.05)), [1, 2, 3], [1, 2, 3]);
		expect(verdict?.status).toBe('spike');
		expect(verdict?.text).toContain('0 of 8 neighbours');
	});
	it('says when nothing makes money, and handles one axis', () => {
		expect(heatmapVerdict(grid(() => -0.01), [1, 2, 3], [1, 2, 3])?.status).toBe('losing');
		const line = [1, 2, 3].map((x) => ({ x, y: null, oos_return: x === 2 ? 0.1 : 0.09 }));
		expect(heatmapVerdict(line, [1, 2, 3], [null])?.text).toContain('2 of 2 neighbours');
	});
	it('says a setting that changes nothing has no effect instead of calling it a plateau', () => {
		const same = { trades: 62, oos_trades: 20, oos_return: 0.082, in_return: -0.002 };
		const line = [0.005, 0.01, 0.015].map((x) => ({ x, y: null, ...same }));
		const verdict = heatmapVerdict(line, [0.005, 0.01, 0.015], [null], { x: 'entry_threshold', y: null });
		expect(verdict?.status).toBe('no_effect');
		expect(verdict?.text).toContain('every value of entry_threshold gave identical trades and returns');
		expect(verdict?.text).toContain('3 of 3 settings make money');
		const flat = [1, 2].flatMap((y) => [1, 2].map((x) => ({ x, y, ...same })));
		expect(heatmapVerdict(flat, [1, 2], [1, 2], { x: 'a', y: 'b' })?.text).toContain('every value of a and b gave identical');
	});
	it('names only the setting that changes nothing when the other one works', () => {
		// Results change down the grid (y) but never across it (x).
		const cells = [1, 2, 3].flatMap((y) => [1, 2, 3].map((x) => ({ x, y, trades: 10 + y, oos_return: 0.02 * y })));
		const verdict = heatmapVerdict(cells, [1, 2, 3], [1, 2, 3], { x: 'exit_threshold', y: 'kc_period' });
		expect(verdict?.status).toBe('no_effect');
		expect(verdict?.text).toContain('every value of exit_threshold gave');
		expect(verdict?.text).not.toContain('kc_period');
	});
	it('does not blame the setting when no cell trades, and still finds plateaus', () => {
		const idle = [1, 2, 3].map((x) => ({ x, y: null, trades: 0, oos_return: 0 }));
		expect(heatmapVerdict(idle, [1, 2, 3], [null])?.status).toBe('losing');
		const working = [1, 2, 3].flatMap((y) => [1, 2, 3].map((x) => ({ x, y, trades: 10 + x + y, oos_return: x === 2 && y === 2 ? 0.1 : 0.08 })));
		expect(heatmapVerdict(working, [1, 2, 3], [1, 2, 3])?.status).toBe('plateau');
	});
});

describe('market grid', () => {
	const row = (net: number, trades = 20): MarketRow => ({
		symbol: 'X', timeframe: '1h', status: 'ok',
		out_of_sample: { trades, net_return: net } as MarketRow['out_of_sample'],
	});

	it('judges whether an edge carries across markets', () => {
		expect(marketVerdict([row(0.05), row(0.02), row(0.01), row(-0.01)])?.status).toBe('broad');
		expect(marketVerdict([row(0.05), row(0.01), row(-0.02), row(-0.01)])?.status).toBe('narrow');
		expect(marketVerdict([row(-0.05), row(-0.02)])?.status).toBe('none');
		const mostlyLosing = marketVerdict([row(0.001), row(-0.05), row(-0.03), row(-0.04)]);
		expect(mostlyLosing?.status).toBe('none');
		expect(mostlyLosing?.text).toContain('1 of 4 markets is profitable');
		const thin = marketVerdict([row(0.05), row(0.1, 3)]);
		expect(thin?.status).toBe('unknown');
		expect(thin?.text).toContain('fewer than 5');
	});

	it('knows which markets have local data, as the backtest loader reads them', () => {
		const availability = marketAvailability([
			{ symbol: 'BTC/USDT', timeframe: '1h' }, { symbol: 'ETH', timeframe: '4h' }, { symbol: 'ADA/BTC', timeframe: '1h' },
		]);
		expect(hasLocalData(availability, 'btcusdt', '1h')).toBe(true);
		expect(hasLocalData(availability, 'ETH/USDC', '4h')).toBe(true);
		expect(hasLocalData(availability, 'BTC/USDT', '4h')).toBe(false);
		expect(hasLocalData(availability, 'ADA/USDT', '1h')).toBe(false);
		expect(baseAsset('SOL-PERP')).toBe('SOL');
	});
});

describe('chunk', () => {
	it('splits work into a few near-equal contiguous requests', () => {
		expect(chunk([1, 2, 3, 4, 5, 6, 7], 3)).toEqual([[1, 2, 3], [4, 5], [6, 7]]);
		expect(chunk([1, 2], 3)).toEqual([[1], [2]]);
		expect(chunk([], 3)).toEqual([]);
	});
});

describe('runPool', () => {
	it('keeps at most the limit in flight and stops taking work once aborted', async () => {
		let inFlight = 0;
		let peak = 0;
		const done: number[] = [];
		const controller = new AbortController();
		await runPool([1, 2, 3, 4, 5, 6], 2, async (n) => {
			inFlight += 1;
			peak = Math.max(peak, inFlight);
			await new Promise((resolve) => setTimeout(resolve, 1));
			inFlight -= 1;
			done.push(n);
			if (n === 3) controller.abort();
		}, controller.signal);
		expect(peak).toBe(2);
		expect(done.length).toBeLessThan(6);
	});
});
