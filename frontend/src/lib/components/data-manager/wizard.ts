/**
 * Input handling for the Get-data and Import wizards: turning the user's
 * choices into request items and checking them before anything is sent. The
 * server stays the authority (estimates, refusals, timeframe inference).
 */
import type { DataStream, DownloadRequestItem, HistoryRequest, ImportPreview, VenueTarget } from '$lib/api/dataManagerTypes';

export type HistoryChoice =
	| { mode: 'all' }
	| { mode: 'years'; years: number }
	| { mode: 'days'; days: number }
	/** UTC: calendar dates (YYYY-MM-DD, both inclusive) or exact ISO timestamps. */
	| { mode: 'range'; start: string; end: string };

export interface MarketDraft {
	symbol: string;
	venue: string;
	timeframes: string[];
	history: HistoryChoice;
	streams: DataStream[];
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$/;
const whenMs = (value: string) => (DATE.test(value) ? Date.parse(`${value}T00:00:00Z`) : DATETIME.test(value) ? Date.parse(value) : NaN);
const todayUtc = (now: number) => new Date(now).toISOString().slice(0, 10);
const isoSeconds = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z');

export function validateHistory(choice: HistoryChoice, now: number = Date.now()): string | null {
	if (choice.mode === 'years') {
		if (!Number.isFinite(choice.years) || choice.years <= 0) return 'Enter how many years of history to download.';
		if (choice.years > 30) return 'Pick 30 years or fewer (or All available).';
		return null;
	}
	if (choice.mode === 'days') {
		if (!Number.isFinite(choice.days) || choice.days <= 0) return 'Enter how many days of history to download.';
		if (choice.days > 30 * 365.25) return 'Pick 30 years or fewer (or All available).';
		return null;
	}
	if (choice.mode === 'range') {
		const start = whenMs(choice.start);
		const end = whenMs(choice.end);
		if (!Number.isFinite(start)) return 'Enter a start date (YYYY-MM-DD, UTC).';
		if (!Number.isFinite(end)) return 'Enter an end date (YYYY-MM-DD, UTC).';
		if (start > end) return 'The start date is after the end date.';
		if (start > now) return 'The start date is in the future.';
		return null;
	}
	return null;
}

export function historyRequest(choice: HistoryChoice, now: number = Date.now()): HistoryRequest {
	if (choice.mode === 'years') return { mode: 'days', days: Math.round(choice.years * 365.25) };
	if (choice.mode === 'days') return { mode: 'days', days: Math.round(choice.days) };
	if (choice.mode === 'range') {
		const start = DATE.test(choice.start) ? `${choice.start}T00:00:00Z` : isoSeconds(whenMs(choice.start));
		const end = DATE.test(choice.end)
			? choice.end >= todayUtc(now) ? isoSeconds(now) : `${choice.end}T23:59:59Z`
			: isoSeconds(Math.min(now, whenMs(choice.end)));
		return { mode: 'range', start, end };
	}
	return { mode: 'all' };
}

export function historyText(choice: HistoryChoice): string {
	if (choice.mode === 'years') return `last ${choice.years} year${choice.years === 1 ? '' : 's'}`;
	if (choice.mode === 'days') return `last ${choice.days} day${choice.days === 1 ? '' : 's'}`;
	if (choice.mode === 'range') return `${choice.start.replace('T00:00:00Z', '')} → ${choice.end.replace('T00:00:00Z', '')} UTC`;
	return 'all available history';
}

// ---------------------------------------------------------------- deep links

/** Streams a download can collect alongside the candles. */
export const DOWNLOAD_STREAMS: DataStream[] = ['funding', 'oi', 'basis', 'ls_ratio', 'taker', 'iv'];

export interface GetDataPrefill {
	symbol: string;
	timeframes: string[];
	venue: string | null;
	history: HistoryChoice | null;
	streams: DataStream[] | null;
}

/** Reads a Get-data deep link (the readiness "fix" links use it):
 * `?symbol=BTC/USDT|BTC-USDT&timeframe=1h&venue=canonical|source:market
 *  &history=all|<N>d|<startISO>..<endISO>&streams=funding,oi,...` */
export function parseGetDataQuery(params: URLSearchParams): GetDataPrefill | null {
	const raw = params.get('symbol');
	if (!raw?.trim()) return null;
	const timeframe = params.get('timeframe')?.trim();
	const venue = params.get('venue')?.trim() || null;
	const historyParam = params.get('history')?.trim() ?? '';
	let history: HistoryChoice | null = null;
	const days = /^(\d+(?:\.\d+)?)d$/i.exec(historyParam);
	const range = /^(.+?)\.\.(.+)$/.exec(historyParam);
	if (historyParam === 'all') history = { mode: 'all' };
	else if (days) history = { mode: 'days', days: Number(days[1]) };
	else if (range && Number.isFinite(whenMs(range[1])) && Number.isFinite(whenMs(range[2]))) history = { mode: 'range', start: range[1], end: range[2] };
	const streamsParam = params.get('streams');
	const streams = streamsParam == null
		? null
		: streamsParam.split(',').map((s) => s.trim()).filter((s): s is DataStream => DOWNLOAD_STREAMS.includes(s as DataStream));
	return { symbol: normalizeSymbol(raw), timeframes: timeframe ? [timeframe] : [], venue, history, streams };
}

/** One item per timeframe. Perp add-ons (funding, OI, basis) ride on a single
 * item, the 1h one when chosen, so they are collected once, not per timeframe. */
export function buildDownloadItems(draft: MarketDraft, now: number = Date.now()): DownloadRequestItem[] {
	const history = historyRequest(draft.history, now);
	const carrier = draft.timeframes.includes('1h') ? '1h' : draft.timeframes[0];
	return draft.timeframes.map((timeframe) => ({
		symbol: draft.symbol,
		timeframe,
		venue: draft.venue,
		history,
		...(draft.streams.length && timeframe === carrier ? { streams: [...draft.streams] } : {}),
	}));
}

export function validateMarketDraft(draft: MarketDraft, target: VenueTarget | null, now: number = Date.now()): string[] {
	const errors: string[] = [];
	if (!draft.symbol) errors.push('Choose a market.');
	else if (!target) errors.push('Choose where to download it from.');
	else if (!target.listed) errors.push(`${draft.symbol} is not listed on this venue.`);
	if (!draft.timeframes.length) errors.push('Choose at least one timeframe.');
	const history = validateHistory(draft.history, now);
	if (history) errors.push(history);
	return errors;
}

// ---------------------------------------------------------------- import

export interface ImportDraft {
	symbol: string;
	timeframe: string;
	mode: 'new' | 'patch';
	conflict_policy: 'keep_existing' | 'overwrite';
}

export function defaultImportMode(preview: ImportPreview | null): 'new' | 'patch' {
	return preview?.target?.exists ? 'patch' : 'new';
}

/** Structural checks; the server's own errors (a timeframe that contradicts the
 * file, unknown symbol, ...) arrive in `preview.errors` and are shown as-is. */
export function validateImport(preview: ImportPreview | null, draft: ImportDraft): { errors: string[]; warnings: string[] } {
	const errors: string[] = [];
	const warnings: string[] = [];
	if (!preview) return { errors: ['Choose a file to import.'], warnings };
	errors.push(...preview.errors);
	if (!preview.required_ok) errors.push('Match the timestamp, open, high, low and close columns.');
	if (!draft.symbol.trim()) errors.push('Enter the symbol this file holds.');
	if (!draft.timeframe) errors.push('Choose the timeframe.');
	const target = preview.target;
	if (draft.symbol.trim() && draft.timeframe) {
		if (!target || target.symbol !== normalizeSymbol(draft.symbol) || target.timeframe !== draft.timeframe) {
			errors.push('Check the file against this symbol and timeframe first.');
		} else if (draft.mode === 'new' && target.exists) {
			errors.push(`${target.symbol} ${target.timeframe} already exists. Choose “Add bars to it” to patch it.`);
		} else if (draft.mode === 'patch' && !target.exists) {
			errors.push(`Nothing is stored for ${target.symbol} ${target.timeframe} yet. Choose “New series”.`);
		} else if (draft.mode === 'patch') {
			if (target.overlap.conflicting && draft.conflict_policy === 'overwrite') {
				warnings.push(`${target.overlap.conflicting.toLocaleString('en-US')} stored bars will be replaced by the file’s values.`);
			}
			if (target.destination === 'canonical') {
				warnings.push('This adds bars to the research series. Patched bars are marked in its provenance.');
			}
		}
	}
	if (preview.inferred_timeframe && draft.timeframe && preview.inferred_timeframe !== draft.timeframe) {
		warnings.push(`The file’s bars look ${preview.inferred_timeframe} apart, not ${draft.timeframe}.`);
	}
	return { errors: [...new Set(errors)], warnings };
}

/** "btc/usdt" -> "BTC-USDT" (the file-system form the API uses). */
export function normalizeSymbol(value: string): string {
	return value.trim().toUpperCase().replace(/[/_ ]+/g, '-');
}
