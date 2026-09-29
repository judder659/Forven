import { beforeEach, describe, expect, it, vi } from 'vitest';

const core = vi.hoisted(() => ({ fetchApi: vi.fn() }));
vi.mock('$lib/api/core', () => ({ fetchApi: core.fetchApi }));

import {
	checkReadiness,
	getSeriesFingerprint,
	getStrategyReadiness,
	getVenueDivergence,
	readinessFixHref,
	specStreams,
} from '../lib/api/dataReadiness';

describe('data readiness client', () => {
	beforeEach(() => {
		core.fetchApi.mockReset();
		core.fetchApi.mockResolvedValue({});
	});

	it('calls one endpoint per function', async () => {
		await getStrategyReadiness('S 01');
		await checkReadiness({ symbol: 'BTC/USDT', timeframe: '1h', streams: ['funding'] });
		await getSeriesFingerprint('BTC-USDT', '1h');
		await getVenueDivergence('ETH-USDT', '4h');
		expect(core.fetchApi.mock.calls).toEqual([
			['/data/readiness/strategy/S%2001'],
			['/data/readiness', { method: 'POST', body: JSON.stringify({ symbol: 'BTC/USDT', timeframe: '1h', streams: ['funding'] }) }],
			['/data/series/BTC-USDT/1h/fingerprint?venue=canonical'],
			['/data/divergence/ETH-USDT?timeframe=4h'],
		]);
	});

	it('links a fix to the Get-data page with its download request', () => {
		expect(
			readinessFixHref({
				action: 'download',
				label: 'Download open interest for BTC-USDT',
				request: { symbol: 'BTC-USDT', timeframe: '1h', venue: 'canonical', history: { mode: 'days', days: 730 }, streams: ['oi', 'funding'] },
			}),
		).toBe('/data/get?symbol=BTC-USDT&timeframe=1h&venue=canonical&history=730d&streams=oi%2Cfunding');
		expect(
			readinessFixHref({
				action: 'extend_history',
				label: 'x',
				request: { symbol: 'ETH-USDT', timeframe: '4h', venue: 'hyperliquid:perp', history: { mode: 'all' } },
			}),
		).toBe('/data/get?symbol=ETH-USDT&timeframe=4h&venue=hyperliquid%3Aperp&history=all');
		expect(readinessFixHref({ action: 'none', label: 'x' })).toBeNull();
		expect(readinessFixHref(null)).toBeNull();
	});

	it('derives the streams a rule spec reads', () => {
		const spec = {
			indicators: [
				{ id: 'fz', kind: 'funding_zscore', params: { length: 96 } },
				{ id: 'rsi', kind: 'rsi', params: { length: 14 } },
			],
			entry_long: {
				logic: 'and',
				conditions: [
					{ left: 'fz', op: '<', right: -1.5 },
					{ logic: 'or', conditions: [{ left: { series: 'ls_ratio' }, op: '>', right: 1 }] },
				],
			},
			exit_long: { conditions: [{ left: 'long_liq_usd', op: '>', right: { param: 'x' } }] },
		};
		expect(specStreams(spec)).toEqual(['funding', 'liquidations', 'ls_ratio']);
		expect(specStreams({ indicators: [{ id: 'rsi', kind: 'rsi' }], entry_long: { conditions: [{ left: 'rsi', op: '<', right: 30 }] } })).toEqual([]);
		expect(specStreams(null)).toEqual([]);
	});
});
