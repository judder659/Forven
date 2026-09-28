import { beforeEach, describe, expect, it, vi } from 'vitest';
import {
	cancelJob,
	checkDelete,
	commitImport,
	dataLogExportUrl,
	deleteSeries,
	estimateDownloads,
	extendHistory,
	freezeSeries,
	getAcquireTargets,
	getCatalog,
	getDataLog,
	getSeriesBars,
	getSeriesDetail,
	getSeriesRows,
	getSlaCensus,
	getStreamPoints,
	listJobs,
	previewImport,
	purgeTrash,
	reclaimStorage,
	refreshSeries,
	resolveIdentity,
	restoreTrashItem,
	retryJob,
	seedUniverse,
	startDownloads,
	toQuery,
} from '../lib/api/dataManager';
import { isRouteMissingError } from '../lib/api/core';

const fetchMock = global.fetch as unknown as ReturnType<typeof vi.fn>;

function respond(body: unknown, status = 200) {
	fetchMock.mockResolvedValueOnce({
		ok: status < 400,
		status,
		statusText: status === 404 ? 'Not Found' : 'OK',
		json: async () => body,
		text: async () => JSON.stringify(body),
		blob: async () => new Blob([JSON.stringify(body)]),
	});
}

function lastCall(): { url: string; init: RequestInit } {
	const [url, init] = fetchMock.mock.calls.at(-1) as [string, RequestInit];
	return { url, init };
}

const job = { id: 'dj-1', kind: 'tail_refresh', status: 'queued' };

describe('dataManager API client', () => {
	beforeEach(() => fetchMock.mockReset());

	it('drops empty params and repeats list params', () => {
		expect(toQuery({ q: '', a: undefined, b: null, tier: ['live', 'paper'], limit: 50, flag: false })).toBe('?tier=live&tier=paper&limit=50&flag=false');
		expect(toQuery({})).toBe('');
	});

	it('builds the catalog query', async () => {
		respond({ total: 0, rows: [], facets: {}, generated_at: '' });
		await getCatalog({ q: 'btc', tier: ['live', 'paper'], state: ['late'], sort: 'priority', order: 'desc', limit: 500, offset: 0 });
		const { url, init } = lastCall();
		expect(url).toBe('/api/data/catalog?q=btc&tier=live&tier=paper&state=late&sort=priority&order=desc&limit=500&offset=0');
		expect(init.method ?? 'GET').toBe('GET');
	});

	it('addresses a series by its file-system symbol and defaults venue/stream', async () => {
		respond({});
		await getSeriesDetail({ symbol: 'BTC-USDT', timeframe: '1h' });
		expect(lastCall().url).toBe('/api/data/series/BTC-USDT/1h?stream=ohlcv&venue=canonical');
		respond({});
		await getSeriesBars({ symbol: 'BTC-USDT', timeframe: '1h', venue: 'hyperliquid:perp' }, { start: '2026-01-01T00:00:00Z', max_points: 3000 });
		expect(lastCall().url).toBe('/api/data/series/BTC-USDT/1h/bars?venue=hyperliquid%3Aperp&start=2026-01-01T00%3A00%3A00Z&max_points=3000');
		respond({});
		await getSeriesRows({ symbol: 'ETH-USDT', timeframe: '8h', stream: 'funding' }, { limit: 100, offset: 200 });
		expect(lastCall().url).toBe('/api/data/series/ETH-USDT/8h/rows?venue=canonical&stream=funding&limit=100&offset=200');
		respond({});
		await getStreamPoints('ETH-USDT', 'oi', { timeframe: '1h', max_points: 120 });
		expect(lastCall().url).toBe('/api/data/streams/ETH-USDT/oi/points?timeframe=1h&max_points=120');
	});

	it('reads the census, identity and log with their params', async () => {
		respond({});
		await getSlaCensus({ limit_worst: 100 });
		expect(lastCall().url).toBe('/api/data/sla?limit_worst=100');
		respond({ query: 'btc', candidates: [] });
		await resolveIdentity('btc/usdt');
		expect(lastCall().url).toBe('/api/data/identity/resolve?q=btc%2Fusdt');
		respond({ total: 0, entries: [] });
		await getDataLog({ category: ['user', 'incident'], level: ['error'], symbol: 'BTC-USDT', limit: 50, offset: 50 });
		expect(lastCall().url).toBe('/api/data/log?category=user&category=incident&level=error&symbol=BTC-USDT&limit=50&offset=50');
		respond({ total: 0, jobs: [] });
		await listJobs({ status: ['running', 'queued'], origin: 'strategy:', routine: false, limit: 20 });
		expect(lastCall().url).toBe('/api/data/jobs?status=running&status=queued&origin=strategy%3A&routine=false&limit=20');
		respond({});
		await getAcquireTargets('SOL-USDT');
		expect(lastCall().url).toBe('/api/data/acquire/targets?symbol=SOL-USDT');
	});

	it('export URL carries the filters but not paging', () => {
		expect(dataLogExportUrl({ category: ['routine'], q: 'tick', limit: 50, offset: 100 })).toBe('/api/data/log/export?category=routine&q=tick');
	});

	it('posts JSON bodies and unwraps { job }', async () => {
		respond({ job });
		const refreshed = await refreshSeries({ scope: 'late_live_paper', mode: 'refresh' });
		expect(refreshed).toEqual(job);
		const { url, init } = lastCall();
		expect(url).toBe('/api/data/sla/refresh');
		expect(init.method).toBe('POST');
		expect(JSON.parse(String(init.body))).toEqual({ scope: 'late_live_paper', mode: 'refresh' });
		expect(new Headers(init.headers).get('Content-Type')).toBe('application/json');

		respond({ updated: 2 });
		await freezeSeries({ series: [{ symbol: 'ALT-BTC', timeframe: '1h', stream: 'ohlcv', venue: 'canonical' }], frozen: true, reason: 'dead' });
		expect(lastCall().url).toBe('/api/data/sla/freeze');

		respond({ job });
		await extendHistory({ symbols: ['XRP-USDT'] });
		expect(lastCall().url).toBe('/api/data/history/extend');

		respond({ job });
		await reclaimStorage({ kind: 'backups', item_ids: 'all', confirm: 'reclaim backups' });
		expect(JSON.parse(String(lastCall().init.body))).toEqual({ kind: 'backups', item_ids: 'all', confirm: 'reclaim backups' });
	});

	it('accepts a bare job row from cancel and retry', async () => {
		respond(job);
		expect(await cancelJob('dj-1')).toEqual(job);
		expect(lastCall().url).toBe('/api/data/jobs/dj-1/cancel');
		respond({ job: { ...job, id: 'dj-2' } });
		expect((await retryJob('dj-1')).id).toBe('dj-2');
		expect(lastCall().url).toBe('/api/data/jobs/dj-1/retry');
	});

	it('covers the remaining operations endpoints', async () => {
		respond({ status: 'started', job });
		await seedUniverse();
		expect(lastCall().url).toBe('/api/data/universe/seed');
		respond({});
		await restoreTrashItem('tr-1');
		expect(lastCall().url).toBe('/api/data/trash/tr-1/restore');
		respond({});
		await purgeTrash({ item_ids: 'all', confirm: 'empty trash' });
		expect(lastCall().url).toBe('/api/data/trash/purge');
		respond({});
		await checkDelete({ symbol: 'BTC-USDT', timeframe: '1h', stream: 'ohlcv', venue: 'canonical' });
		expect(lastCall().url).toBe('/api/data/delete/check?symbol=BTC-USDT&timeframe=1h&stream=ohlcv&venue=canonical');
		respond({ trashed: [], skipped: [] });
		await deleteSeries({ series: [{ symbol: 'BTC-USDT', timeframe: '1h', stream: 'ohlcv', venue: 'canonical' }], confirm: 'delete BTC-USDT 1h' });
		expect(lastCall().url).toBe('/api/data/delete');
		const items = [{ symbol: 'BTC-USDT', timeframe: '1h', venue: 'canonical', history: { mode: 'all' as const } }];
		respond({ estimates: [] });
		await estimateDownloads(items);
		expect(lastCall().url).toBe('/api/data/acquire/estimate');
		expect(JSON.parse(String(lastCall().init.body))).toEqual({ items });
		respond({ jobs: [] });
		await startDownloads(items);
		expect(lastCall().url).toBe('/api/data/acquire/downloads');
	});

	it('sends imports as multipart without a JSON content type', async () => {
		const file = new File(['Date,Open\n'], 'xau.csv', { type: 'text/csv' });
		respond({ filename: 'xau.csv' });
		await previewImport(file, { symbol: 'XAU-USD', timeframe: '1h', timezone: 'UTC', mapping: { timestamp: 'Date', open: 'Open' } });
		let { url, init } = lastCall();
		expect(url).toBe('/api/data/acquire/import/preview');
		expect(init.body).toBeInstanceOf(FormData);
		expect(new Headers(init.headers).has('Content-Type')).toBe(false);
		let form = init.body as FormData;
		expect(form.get('symbol')).toBe('XAU-USD');
		expect(form.get('timezone')).toBe('UTC');
		expect(JSON.parse(String(form.get('mapping_json')))).toEqual({ timestamp: 'Date', open: 'Open' });

		respond({ rows_written: 1 });
		await commitImport(file, { symbol: 'XAU-USD', timeframe: '1h', mode: 'patch', conflict_policy: 'keep_existing' }, { timezone: 'UTC' });
		({ url, init } = lastCall());
		expect(url).toBe('/api/data/acquire/import');
		form = init.body as FormData;
		expect(form.get('mode')).toBe('patch');
		expect(form.get('conflict_policy')).toBe('keep_existing');
		expect(form.get('file')).toBeInstanceOf(File);
		expect(form.has('mapping_json')).toBe(false);
	});

	it('a bare 404 reads as a route that is not deployed; a detailed one does not', async () => {
		respond({ detail: 'Not Found' }, 404);
		const missing = await getSlaCensus().catch((e) => e);
		expect(isRouteMissingError(missing)).toBe(true);
		respond({ detail: 'Series BTC-USDT 7m not found' }, 404);
		const notFound = await getSeriesDetail({ symbol: 'BTC-USDT', timeframe: '7m' }).catch((e) => e);
		expect(isRouteMissingError(notFound)).toBe(false);
	});
});
