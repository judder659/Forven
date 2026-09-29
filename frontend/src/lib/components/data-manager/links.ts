/** Links inside the Data Manager page. Series are addressed by their
 * file-system symbol ("BTC-USDT"), never the display form ("BTC/USDT"). */
import type { SymbolCandidate } from '$lib/api/dataManagerTypes';

export const DM = '/data';

export function seriesHref(ref: { symbol: string; timeframe: string; stream?: string; venue?: string }): string {
	const params = new URLSearchParams();
	if (ref.stream && ref.stream !== 'ohlcv') params.set('stream', ref.stream);
	if (ref.venue && ref.venue !== 'canonical') params.set('venue', ref.venue);
	const query = params.toString();
	return `${DM}/series/${encodeURIComponent(ref.symbol)}/${encodeURIComponent(ref.timeframe)}${query ? `?${query}` : ''}`;
}

export function catalogHref(filters: Record<string, string | string[]> = {}): string {
	const params = new URLSearchParams();
	for (const [key, value] of Object.entries(filters)) {
		for (const item of Array.isArray(value) ? value : [value]) if (item) params.append(key, item);
	}
	const query = params.toString();
	return `${DM}/catalog${query ? `?${query}` : ''}`;
}

const PREFERRED_TF = ['1h', '4h', '1d', '15m', '5m', '1m', '30m', '1w'];

/** Where a symbol search result goes: its best stored series, or Get data when nothing is stored. */
export function candidateHref(candidate: Pick<SymbolCandidate, 'symbol' | 'stored'>): string {
	const candles = candidate.stored.filter((s) => s.stream === 'ohlcv' && s.rows > 0);
	const pool = candles.length ? candles : candidate.stored.filter((s) => s.rows > 0);
	if (!pool.length) return `${DM}/get?symbol=${encodeURIComponent(candidate.symbol)}`;
	const rank = (s: (typeof pool)[number]) => (s.venue === 'canonical' ? 0 : 100) + (PREFERRED_TF.indexOf(s.timeframe) + 1 || 50);
	const best = [...pool].sort((a, b) => rank(a) - rank(b))[0];
	return seriesHref({ symbol: candidate.symbol, timeframe: best.timeframe, stream: best.stream, venue: best.venue });
}
