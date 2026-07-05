/**
 * Live unrealized P&L math shared by the All Trades blotter and the global
 * header P&L ticker: mark an OPEN trade to the latest WS tick price.
 */
import type { ForvenTrade } from '$lib/api';

export function toNumber(value: unknown): number | null {
	if (value === null || value === undefined || value === '') return null;
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

/** Current live mark for an asset from the WS price feed (keyed by plain asset,
 * e.g. BTC). Falls back to a separator/quote-insensitive match. */
export function livePrice(asset: string | undefined, prices: Record<string, number>): number | null {
	const key = String(asset ?? '').trim().toUpperCase();
	if (!key) return null;
	const direct = Number(prices[key]);
	if (Number.isFinite(direct) && direct > 0) return direct;
	const norm = (s: string) => s.toUpperCase().replace(/[-_/]/g, '').replace(/(USDT|USD|PERP)$/, '');
	const want = norm(key);
	for (const [k, v] of Object.entries(prices)) {
		if (norm(k) !== want) continue;
		const n = Number(v);
		if (Number.isFinite(n) && n > 0) return n;
	}
	return null;
}

/** Live unrealized $ P&L for an OPEN trade (price move × size, direction-signed).
 * Null when no live price is available. */
export function liveUnrealizedUsd(t: ForvenTrade, prices: Record<string, number>): number | null {
	const entry = toNumber(t.fill_entry_price) ?? toNumber(t.entry_price);
	const size = toNumber(t.size);
	const px = livePrice(t.asset, prices);
	if (entry === null || size === null || px === null) return null;
	const sign = String(t.direction ?? '').toLowerCase() === 'short' ? -1 : 1;
	return (px - entry) * size * sign;
}
