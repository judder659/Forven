import { describe, expect, it } from 'vitest';
import { toNumber, livePrice, liveUnrealizedUsd } from '../lib/utils/livePnl';

describe('livePnl', () => {
	it('toNumber parses finite numbers and rejects blanks/garbage', () => {
		expect(toNumber(1.5)).toBe(1.5);
		expect(toNumber('2')).toBe(2);
		expect(toNumber('')).toBeNull();
		expect(toNumber(null)).toBeNull();
		expect(toNumber(undefined)).toBeNull();
		expect(toNumber('abc')).toBeNull();
		expect(toNumber(Infinity)).toBeNull();
	});

	it('livePrice resolves the plain-asset key directly', () => {
		expect(livePrice('BTC', { BTC: 50_000 })).toBe(50_000);
	});

	it('livePrice falls back to separator/quote-insensitive matches', () => {
		expect(livePrice('BTC', { 'BTC-USDT': 50_000 })).toBe(50_000);
		expect(livePrice('BTC-USDT', { BTC: 50_000 })).toBe(50_000);
		expect(livePrice('ETH', { 'ETH/USD': 3_000 })).toBe(3_000);
	});

	it('livePrice returns null for unknown assets and non-positive marks', () => {
		expect(livePrice('DOGE', { BTC: 50_000 })).toBeNull();
		expect(livePrice('BTC', { BTC: 0 })).toBeNull();
		expect(livePrice('', { BTC: 50_000 })).toBeNull();
	});

	it('marks a long to the live price with the right sign', () => {
		const trade = { asset: 'BTC', direction: 'long', size: 0.1, fill_entry_price: 50_000 };
		expect(liveUnrealizedUsd(trade, { BTC: 51_000 })).toBeCloseTo(100);
		expect(liveUnrealizedUsd(trade, { BTC: 49_000 })).toBeCloseTo(-100);
	});

	it('marks a short with the inverted sign', () => {
		const trade = { asset: 'ETH', direction: 'short', size: 1, fill_entry_price: 3_000 };
		expect(liveUnrealizedUsd(trade, { ETH: 2_900 })).toBeCloseTo(100);
		expect(liveUnrealizedUsd(trade, { ETH: 3_100 })).toBeCloseTo(-100);
	});

	it('prefers the fill entry price over the signal entry price', () => {
		const trade = { asset: 'BTC', direction: 'long', size: 1, fill_entry_price: 50_100, entry_price: 50_000 };
		expect(liveUnrealizedUsd(trade, { BTC: 50_100 })).toBeCloseTo(0);
	});

	it('returns null when the entry, size, or live mark is missing', () => {
		expect(liveUnrealizedUsd({ asset: 'BTC', direction: 'long', size: 1 }, { BTC: 50_000 })).toBeNull();
		expect(liveUnrealizedUsd({ asset: 'BTC', direction: 'long', fill_entry_price: 50_000 }, { BTC: 50_000 })).toBeNull();
		expect(liveUnrealizedUsd({ asset: 'BTC', direction: 'long', size: 1, fill_entry_price: 50_000 }, {})).toBeNull();
	});
});
