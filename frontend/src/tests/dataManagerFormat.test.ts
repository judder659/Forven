import { describe, expect, it } from 'vitest';
import {
	compactDuration,
	errorText,
	formatBytes,
	formatCompact,
	formatDuration,
	formatPercent,
	formatRelative,
	formatUtc,
	historyLength,
	jobEtaSeconds,
	jobSeriesText,
	lagCaption,
	originLabel,
	progressText,
	stateChipClass,
	venueLabel,
	venueShort,
} from '../lib/components/data-manager/format';

const NOW = Date.parse('2026-09-28T19:22:00Z');

describe('Data Manager formatting', () => {
	it('shows UTC on absolute timestamps', () => {
		expect(formatUtc('2026-09-28T18:30:05Z')).toBe('2026-09-28 18:30 UTC');
		expect(formatUtc('2026-09-28T18:30:05Z', { seconds: true })).toBe('2026-09-28 18:30:05 UTC');
		expect(formatUtc('2026-09-28T18:30:05Z', { date: true })).toBe('2026-09-28');
		expect(formatUtc(null)).toBe('—');
	});

	it('says how long ago, and how long until', () => {
		expect(formatRelative('2026-09-28T19:21:48Z', NOW)).toBe('12 s ago');
		expect(formatRelative('2026-09-28T18:30:00Z', NOW)).toBe('52 min ago');
		expect(formatRelative('2026-09-28T14:00:00Z', NOW)).toBe('5 h ago');
		expect(formatRelative('2026-09-25T19:22:00Z', NOW)).toBe('3 d ago');
		expect(formatRelative('2026-09-28T19:22:18Z', NOW)).toBe('in 18 s');
		expect(formatRelative('2026-09-28T19:21:59Z', NOW)).toBe('just now');
		expect(formatRelative('2019-09-08T00:00:00Z', NOW)).toBe('7 y ago');
	});

	it('writes durations the way the spec reads them', () => {
		expect(formatDuration(45)).toBe('45 s');
		expect(formatDuration(45 * 60)).toBe('45 min');
		expect(formatDuration(5 * 3600 + 12 * 60)).toBe('5 h 12 m');
		expect(formatDuration(3 * 3600)).toBe('3 h');
		expect(formatDuration(3 * 86400 + 4 * 3600)).toBe('3 d 4 h');
		expect(formatDuration(null)).toBe('—');
		expect(compactDuration(5.5 * 3600)).toBe('5.5 h');
		expect(compactDuration(2 * 3600)).toBe('2 h');
		expect(compactDuration(52 * 60)).toBe('52 min');
		expect(lagCaption({ lag_seconds: 5.5 * 3600, allowed_seconds: 2 * 3600 })).toBe('5.5 h / 2 h');
		expect(lagCaption({ lag_seconds: null, allowed_seconds: 45 * 60 })).toBe('no data / 45 min');
	});

	it('formats sizes, counts and shares', () => {
		expect(formatBytes(641 * 1024 * 1024)).toBe('641 MB');
		expect(formatBytes(1.26 * 1024 ** 3)).toBe('1.26 GB');
		expect(formatBytes(512)).toBe('512 B');
		expect(formatCompact(3_546_036)).toBe('3.5 M');
		expect(formatCompact(61_849)).toBe('61.8 k');
		expect(formatCompact(940)).toBe('940');
		expect(formatPercent(1)).toBe('100%');
		expect(formatPercent(0.9984)).toBe('99.8%');
		expect(formatPercent(0.99999)).toBe('99.99%');
		expect(formatPercent(null)).toBe('—');
	});

	it('measures history length', () => {
		expect(historyLength('2020-09-14T00:00:00Z', '2026-09-28T00:00:00Z')).toBe('6.0y');
		expect(historyLength('2019-09-08T00:00:00Z', '2026-09-28T17:00:00Z')).toBe('7.1y');
		expect(historyLength('2026-01-01T00:00:00Z', '2026-09-28T00:00:00Z')).toBe('8mo');
		expect(historyLength('2026-09-05T00:00:00Z', '2026-09-28T00:00:00Z')).toBe('23d');
		expect(historyLength(null, '2026-09-28T00:00:00Z')).toBe('—');
	});

	it('names venues and origins in plain language', () => {
		expect(venueShort('canonical')).toBe('Research');
		expect(venueShort('hyperliquid:perp')).toBe('HL perp');
		expect(venueShort('okx:spot')).toBe('OKX spot');
		expect(venueShort('csv:unknown')).toBe('CSV');
		expect(venueLabel('canonical', 'binanceusdm', 'perp')).toBe('Binance USD-M perp · research series');
		expect(venueLabel('canonical', 'binance', 'spot')).toBe('Binance spot · research series');
		expect(venueLabel('hyperliquid:perp')).toBe('Hyperliquid perp · separate venue series');
		expect(originLabel('user')).toBe('You');
		expect(originLabel('sla')).toBe('Automatic');
		expect(originLabel('strategy:S04928')).toBe('Strategy S04928');
		expect(errorText('rate_limited')).toMatch(/rate-limited/);
		expect(errorText('some_new_code')).toBe('some new code');
	});

	it('pairs every state colour class with a state', () => {
		for (const state of ['fresh', 'late', 'breach', 'frozen', 'missing'] as const) expect(stateChipClass(state)).toBeTruthy();
		expect(stateChipClass('frozen')).toMatch(/repeating-linear-gradient/);
	});

	it('describes job progress and ETA', () => {
		expect(progressText({ done: 61_200, total: 105_120, unit: 'bars' })).toBe('61,200 / 105,120 bars · 58%');
		expect(progressText({ done: 12, total: null, unit: 'series' })).toBe('12 series');
		expect(progressText({ done: 0, total: null, unit: null })).toBe('');
		const running = { status: 'running' as const, started_at: '2026-09-28T19:12:00Z', progress: { done: 25, total: 100, unit: 'months' } };
		expect(jobEtaSeconds(running, NOW)).toBe(1800);
		expect(jobEtaSeconds({ ...running, status: 'queued' }, NOW)).toBeNull();
		expect(jobSeriesText({ series: [{ symbol: 'BTC-USDT', timeframe: '1h', stream: 'ohlcv' }, { symbol: 'ETH-USDT' }] })).toBe('BTC-USDT 1h +1 more');
		expect(jobSeriesText({ series: [{ symbol: 'ETH-USDT', timeframe: '8h', stream: 'funding' }] })).toBe('ETH-USDT 8h funding');
	});
});
