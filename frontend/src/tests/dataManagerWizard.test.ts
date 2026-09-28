import { describe, expect, it } from 'vitest';
import {
	buildDownloadItems,
	defaultImportMode,
	historyRequest,
	historyText,
	normalizeSymbol,
	validateHistory,
	validateImport,
	validateMarketDraft,
	type ImportDraft,
	type MarketDraft,
} from '../lib/components/data-manager/wizard';
import { fixtureImportPreview, fixtureTargets } from '../lib/api/dataManagerFixtures';

const NOW = Date.parse('2026-09-28T19:22:00Z');

describe('Get data: history', () => {
	it('validates each history choice', () => {
		expect(validateHistory({ mode: 'all' }, NOW)).toBeNull();
		expect(validateHistory({ mode: 'years', years: 3 }, NOW)).toBeNull();
		expect(validateHistory({ mode: 'years', years: 0 }, NOW)).toMatch(/how many years/);
		expect(validateHistory({ mode: 'range', start: '2024-01-01', end: '2024-12-31' }, NOW)).toBeNull();
		expect(validateHistory({ mode: 'range', start: '2024-13-01', end: '2024-12-31' }, NOW)).toMatch(/start date/);
		expect(validateHistory({ mode: 'range', start: '2025-01-01', end: '2024-12-31' }, NOW)).toMatch(/after the end/);
		expect(validateHistory({ mode: 'range', start: '2026-10-01', end: '2026-10-02' }, NOW)).toMatch(/future/);
	});

	it('turns choices into UTC requests', () => {
		expect(historyRequest({ mode: 'years', years: 3 }, NOW)).toEqual({ mode: 'days', days: 1096 });
		expect(historyRequest({ mode: 'range', start: '2024-01-01', end: '2024-12-31' }, NOW)).toEqual({ mode: 'range', start: '2024-01-01T00:00:00Z', end: '2024-12-31T23:59:59Z' });
		expect(historyRequest({ mode: 'range', start: '2026-09-01', end: '2026-09-28' }, NOW)).toEqual({ mode: 'range', start: '2026-09-01T00:00:00Z', end: '2026-09-28T19:22:00Z' });
		expect(historyText({ mode: 'range', start: '2024-01-01', end: '2024-12-31' })).toBe('2024-01-01 → 2024-12-31 UTC');
	});
});

describe('Get data: items', () => {
	const draft: MarketDraft = { symbol: 'SOL-USDT', venue: 'canonical', timeframes: ['15m', '1h', '4h'], history: { mode: 'all' }, streams: ['funding', 'oi'] };

	it('one item per timeframe, perp add-ons collected once', () => {
		const items = buildDownloadItems(draft, NOW);
		expect(items.map((i) => i.timeframe)).toEqual(['15m', '1h', '4h']);
		expect(items.filter((i) => i.streams).map((i) => i.timeframe)).toEqual(['1h']);
		expect(items[1].streams).toEqual(['funding', 'oi']);
		expect(buildDownloadItems({ ...draft, timeframes: ['4h', '1d'] }, NOW)[0].streams).toEqual(['funding', 'oi']);
		expect(buildDownloadItems({ ...draft, streams: [] }, NOW).every((i) => !('streams' in i))).toBe(true);
	});

	it('reports what is missing before anything is sent', () => {
		const target = fixtureTargets('SOL-USDT').targets[0];
		expect(validateMarketDraft(draft, target, NOW)).toEqual([]);
		expect(validateMarketDraft({ ...draft, timeframes: [] }, target, NOW)).toEqual(['Choose at least one timeframe.']);
		expect(validateMarketDraft({ ...draft, symbol: '' }, null, NOW)).toContain('Choose a market.');
		expect(validateMarketDraft(draft, { ...target, listed: false }, NOW)[0]).toMatch(/not listed/);
	});
});

describe('Import wizard', () => {
	const fresh = fixtureImportPreview('XAUUSD_1h_2025.csv', { symbol: 'XAU-USD', timeframe: '1h' });
	const draft: ImportDraft = { symbol: 'xau/usd', timeframe: '1h', mode: 'new', conflict_policy: 'keep_existing' };

	it('needs a file and a checked target', () => {
		expect(validateImport(null, draft).errors).toEqual(['Choose a file to import.']);
		expect(validateImport(fresh, draft)).toEqual({ errors: [], warnings: [] });
		expect(validateImport(fresh, { ...draft, timeframe: '4h' }).errors).toContain('Check the file against this symbol and timeframe first.');
		expect(validateImport(fresh, { ...draft, symbol: '' }).errors).toContain('Enter the symbol this file holds.');
	});

	it('keeps new and patch honest', () => {
		expect(defaultImportMode(fresh)).toBe('new');
		expect(validateImport(fresh, { ...draft, mode: 'patch' }).errors[0]).toMatch(/Nothing is stored/);
		const existing = fixtureImportPreview('BTC.csv', { symbol: 'BTC-USDT', timeframe: '1h' });
		expect(defaultImportMode(existing)).toBe('patch');
		expect(validateImport(existing, { ...draft, symbol: 'BTC-USDT', mode: 'new' }).errors[0]).toMatch(/already exists/);
		const overwrite = validateImport(existing, { ...draft, symbol: 'BTC-USDT', mode: 'patch', conflict_policy: 'overwrite' });
		expect(overwrite.errors).toEqual([]);
		expect(overwrite.warnings[0]).toMatch(/186 stored bars will be replaced/);
		expect(overwrite.warnings[1]).toMatch(/research series/);
	});

	it('shows the server’s own errors and the timeframe mismatch', () => {
		const refused = { ...fresh, errors: ['Declared 1d contradicts the file (1h, 96% of rows)'] };
		expect(validateImport(refused, draft).errors[0]).toMatch(/contradicts/);
		const mismatch = { ...fresh, target: { ...fresh.target!, timeframe: '4h' } };
		expect(validateImport(mismatch, { ...draft, timeframe: '4h' }).warnings[0]).toMatch(/look 1h apart, not 4h/);
		expect(validateImport({ ...fresh, required_ok: false }, draft).errors[0]).toMatch(/Match the timestamp/);
		expect(normalizeSymbol(' btc/usdt ')).toBe('BTC-USDT');
	});
});
