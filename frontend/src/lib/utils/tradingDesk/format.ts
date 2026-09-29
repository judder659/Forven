/** Number, money, price and time formatting for the trading desk. Pure functions. */

export const MINUS = '−';

const formatters = new Map<number, Intl.NumberFormat>();
function nf(digits: number): Intl.NumberFormat {
	let fmt = formatters.get(digits);
	if (!fmt) {
		fmt = new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
		formatters.set(digits, fmt);
	}
	return fmt;
}

/** A usable finite number, or null for null/undefined/''/NaN. */
export function num(value: unknown): number | null {
	if (value === null || value === undefined || value === '') return null;
	const n = Number(value);
	return Number.isFinite(n) ? n : null;
}

export function isNum(value: unknown): value is number {
	return num(value) !== null;
}

export function fmtNum(value: unknown, digits = 2): string {
	const n = num(value);
	if (n === null) return '—';
	return `${n < 0 ? MINUS : ''}${nf(digits).format(Math.abs(n))}`;
}

export function fmtUsd(value: unknown, options: { signed?: boolean; digits?: number } = {}): string {
	const n = num(value);
	if (n === null) return '—';
	const sign = n < 0 ? MINUS : options.signed && n > 0 ? '+' : '';
	return `${sign}$${nf(options.digits ?? 2).format(Math.abs(n))}`;
}

/** $3.0B / $412.5M / $98.1K for large dollar amounts (open interest). */
export function fmtCompactUsd(value: unknown): string {
	const n = num(value);
	if (n === null) return '—';
	const abs = Math.abs(n);
	const sign = n < 0 ? MINUS : '';
	if (abs >= 1e9) return `${sign}$${nf(2).format(abs / 1e9)}B`;
	if (abs >= 1e6) return `${sign}$${nf(1).format(abs / 1e6)}M`;
	if (abs >= 1e3) return `${sign}$${nf(1).format(abs / 1e3)}K`;
	return `${sign}$${nf(2).format(abs)}`;
}

/** Decimal places that suit a price: 83,172.0 · 2,679.30 · 118.34 · 0.00012. */
export function pxDigits(price: number): number {
	const abs = Math.abs(price);
	if (abs >= 10000) return 1;
	if (abs >= 100) return 2;
	if (abs >= 1) return 3;
	return 5;
}

export function fmtPx(value: unknown): string {
	const n = num(value);
	return n === null ? '—' : nf(pxDigits(n)).format(n);
}

/** Percent from a value already in percent units (1.5 → "+1.50%"). */
export function fmtPct(value: unknown, digits = 2, signed = true): string {
	const n = num(value);
	if (n === null) return '—';
	const sign = n < 0 ? MINUS : signed && n > 0 ? '+' : '';
	return `${sign}${nf(digits).format(Math.abs(n))}%`;
}

export function fmtQty(value: unknown): string {
	const n = num(value);
	if (n === null) return '—';
	const abs = Math.abs(n);
	const digits = abs >= 100 ? 2 : abs >= 1 ? 3 : abs >= 0.01 ? 4 : 6;
	return nf(digits).format(n).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '');
}

/** Signed basis points with one decimal ("+7.9", "−6.0"). */
export function fmtBps(value: unknown): string {
	const n = num(value);
	if (n === null) return '—';
	return `${n < 0 ? MINUS : n > 0 ? '+' : ''}${nf(1).format(Math.abs(n))}`;
}

/** An hourly funding rate as a percent per hour ("0.0013%/h"). */
export function fmtRateHourly(value: unknown): string {
	const n = num(value);
	if (n === null) return '—';
	return `${n < 0 ? MINUS : ''}${nf(4).format(Math.abs(n * 100))}%/h`;
}

export type Tone = 'pos' | 'neg' | 'flat';

export function tone(value: unknown): Tone {
	const n = num(value);
	if (n === null || n === 0) return 'flat';
	return n > 0 ? 'pos' : 'neg';
}

/** Tailwind text class for a tone, in the app's teal / orange gain-loss palette. */
export function toneClass(value: unknown): string {
	const t = tone(value);
	return t === 'pos' ? 'text-[#5ccac4]' : t === 'neg' ? 'text-[#f2956f]' : 'text-sc-ink2';
}

export function cap1(text: string | null | undefined): string {
	const s = String(text ?? '');
	return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

// ---- time ----

/** Epoch ms from an ISO string (a missing zone means UTC), epoch seconds or ms. */
export function parseTs(value: unknown): number | null {
	if (value === null || value === undefined || value === '') return null;
	if (typeof value === 'number') return value > 1e12 ? value : value * 1000;
	let text = String(value).trim().replace(' ', 'T');
	if (!/[zZ]$|[+-]\d\d:?\d\d$/.test(text)) text += 'Z';
	const ms = Date.parse(text);
	return Number.isNaN(ms) ? null : ms;
}

/** "4m ago", "3h 12m ago", "5d ago". */
export function ago(value: unknown, now: number): string {
	const ms = parseTs(value);
	if (ms === null) return '—';
	const s = Math.max(0, (now - ms) / 1000);
	if (s < 60) return `${Math.floor(s)}s ago`;
	if (s < 3600) return `${Math.floor(s / 60)}m ago`;
	if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m ago`;
	return `${Math.floor(s / 86400)}d ago`;
}

/** A countdown or duration: "46m 05s", "3h 12m", "4d 2h". */
export function dur(ms: number): string {
	const s = Math.max(0, Math.floor(ms / 1000));
	const h = Math.floor(s / 3600);
	const m = Math.floor((s % 3600) / 60);
	const sec = s % 60;
	if (h >= 48) return `${Math.floor(h / 24)}d ${h % 24}h`;
	if (h > 0) return `${h}h ${String(m).padStart(2, '0')}m`;
	return `${m}m ${String(sec).padStart(2, '0')}s`;
}

/** `dur` without the seconds once a minute or more is left, for tight spaces. */
export function durShort(ms: number): string {
	const s = Math.max(0, Math.floor(ms / 1000));
	return s < 60 ? `${s}s` : s < 3600 ? `${Math.floor(s / 60)}m` : dur(ms);
}

export function fmtTime(value: unknown): string {
	const ms = parseTs(value);
	return ms === null ? '—' : new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
}

export function fmtDay(value: unknown): string {
	const ms = parseTs(value);
	return ms === null ? '—' : new Date(ms).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function fmtDateTime(value: unknown): string {
	const ms = parseTs(value);
	return ms === null ? '—' : `${fmtDay(ms)} ${fmtTime(ms)}`;
}

export const TIMEFRAME_SECONDS: Record<string, number> = {
	'1m': 60,
	'5m': 300,
	'15m': 900,
	'30m': 1800,
	'1h': 3600,
	'2h': 7200,
	'4h': 14400,
	'8h': 28800,
	'12h': 43200,
	'1d': 86400,
};

export function timeframeMs(timeframe: string | null | undefined): number {
	return (TIMEFRAME_SECONDS[String(timeframe ?? '').toLowerCase()] ?? 3600) * 1000;
}

/** Bars close on UTC-aligned boundaries (4h bars at 00/04/08/12/16/20 UTC). */
export function nextBarClose(timeframe: string | null | undefined, now: number): number {
	const step = timeframeMs(timeframe);
	return Math.ceil((now + 1) / step) * step;
}

export function lastBarClose(timeframe: string | null | undefined, now: number): number {
	const step = timeframeMs(timeframe);
	return Math.floor(now / step) * step;
}
