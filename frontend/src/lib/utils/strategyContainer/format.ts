// Display formatting for the strategy container. Negative numbers use a real minus
// sign, window and bar dates are UTC (the candles are), and a missing value renders
// as an em dash rather than a zero.

export const MINUS = '−';
export const DASH = '—';
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export function isNum(value: unknown): value is number {
	return typeof value === 'number' && Number.isFinite(value);
}

/** A finite number, or null. `null`, `''` and booleans are missing, not zero. */
export function toNumber(value: unknown): number | null {
	if (typeof value === 'number') return Number.isFinite(value) ? value : null;
	if (value === null || value === undefined || typeof value === 'boolean') return null;
	if (typeof value === 'string' && !value.trim()) return null;
	const parsed = Number(value);
	return Number.isFinite(parsed) ? parsed : null;
}

function magnitude(value: number, digits: number): string {
	return Math.abs(value).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
}

function signOf(value: number, digits: number, signed: boolean): string {
	const rounded = Number(value.toFixed(digits));
	if (rounded < 0) return MINUS;
	return signed && rounded > 0 ? '+' : '';
}

/** A percent that is already in percent points (12.4 → "+12.4%"). */
export function fmtPct(value: number | null | undefined, digits = 1, signed = true): string {
	if (!isNum(value)) return DASH;
	return `${signOf(value, digits, signed)}${magnitude(value, digits)}%`;
}

/** A fraction shown as a percent (0.124 → "+12.4%"). */
export function fmtFraction(value: number | null | undefined, digits = 1, signed = true): string {
	return isNum(value) ? fmtPct(value * 100, digits, signed) : DASH;
}

export function fmtNum(value: number | null | undefined, digits = 2): string {
	if (!isNum(value)) return DASH;
	return `${signOf(value, digits, false)}${magnitude(value, digits)}`;
}

export function fmtUsd(value: number | null | undefined, digits = 2, signed = true): string {
	if (!isNum(value)) return DASH;
	return `${signOf(value, digits, signed)}$${magnitude(value, digits)}`;
}

export function parseTimestamp(value: unknown): number | null {
	if (typeof value !== 'string' || !value.trim()) return null;
	const parsed = Date.parse(value.trim().replace(' ', 'T'));
	return Number.isFinite(parsed) ? parsed : null;
}

/** "Dec 23, 2020" in UTC. */
export function fmtDateUtc(value: unknown): string {
	const ts = typeof value === 'number' ? value : parseTimestamp(value);
	if (ts === null) return DASH;
	return new Date(ts).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' });
}

/** "Dec 2020" in UTC. */
export function fmtMonthYear(value: unknown): string {
	const ts = typeof value === 'number' ? value : parseTimestamp(value);
	if (ts === null) return DASH;
	const d = new Date(ts);
	return `${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

/** "2024-07-03 21:00" in UTC, for bar timestamps. */
export function fmtUtcStamp(value: unknown): string {
	const ts = typeof value === 'number' ? value : parseTimestamp(value);
	if (ts === null) return DASH;
	return new Date(ts).toISOString().slice(0, 16).replace('T', ' ');
}

export function monthLabel(month: number): string {
	return MONTHS[month] ?? '';
}

/** Sign class for a signed value: 'pos', 'neg' or '' (flat or missing). */
export function signTone(value: number | null | undefined): 'pos' | 'neg' | '' {
	if (!isNum(value)) return '';
	const rounded = Number(value.toFixed(6));
	return rounded > 0 ? 'pos' : rounded < 0 ? 'neg' : '';
}

/** Tailwind text classes for signed values, CVD-safe (teal gain, red-orange loss). */
export function signClass(value: number | null | undefined): string {
	const tone = signTone(value);
	return tone === 'pos' ? 'text-[#5ccac4]' : tone === 'neg' ? 'text-[#f2956f]' : '';
}

/** "eth_vt63503_volume_thrust_iv_confirm" → "ETH Volume Thrust IV Confirm". */
export function humanizeStrategyType(type: string | null | undefined): string {
	const acronyms: Record<string, string> = { eth: 'ETH', btc: 'BTC', sol: 'SOL', xrp: 'XRP', iv: 'IV', oi: 'OI', kc: 'KC', rsi: 'RSI', macd: 'MACD', atr: 'ATR', ema: 'EMA', sma: 'SMA', adx: 'ADX', vwap: 'VWAP', bb: 'BB' };
	const words = String(type ?? '')
		.split(/[_\s-]+/)
		.filter((word) => word && !/^[a-z]{1,3}\d{3,}$/i.test(word) && !/^s\d{4,}$/i.test(word));
	return words.map((word) => acronyms[word.toLowerCase()] ?? word.charAt(0).toUpperCase() + word.slice(1).toLowerCase()).join(' ');
}
