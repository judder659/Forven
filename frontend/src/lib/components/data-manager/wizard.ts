/**
 * Input handling for the Get-data and Import wizards: turning the user's
 * choices into request items and checking them before anything is sent. The
 * server stays the authority (estimates, refusals, timeframe inference).
 */
import type { DataStream, DownloadRequestItem, HistoryRequest, ImportPreview, VenueTarget } from '$lib/api/dataManagerTypes';

export type HistoryChoice =
	| { mode: 'all' }
	| { mode: 'years'; years: number }
	/** UTC calendar dates, YYYY-MM-DD, both inclusive. */
	| { mode: 'range'; start: string; end: string };

export interface MarketDraft {
	symbol: string;
	venue: string;
	timeframes: string[];
	history: HistoryChoice;
	streams: DataStream[];
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;
const dayMs = (date: string) => Date.parse(`${date}T00:00:00Z`);
const todayUtc = (now: number) => new Date(now).toISOString().slice(0, 10);

export function validateHistory(choice: HistoryChoice, now: number = Date.now()): string | null {
	if (choice.mode === 'years') {
		if (!Number.isFinite(choice.years) || choice.years <= 0) return 'Enter how many years of history to download.';
		if (choice.years > 30) return 'Pick 30 years or fewer (or All available).';
		return null;
	}
	if (choice.mode === 'range') {
		if (!DATE.test(choice.start) || !Number.isFinite(dayMs(choice.start))) return 'Enter a start date (YYYY-MM-DD, UTC).';
		if (!DATE.test(choice.end) || !Number.isFinite(dayMs(choice.end))) return 'Enter an end date (YYYY-MM-DD, UTC).';
		if (dayMs(choice.start) > dayMs(choice.end)) return 'The start date is after the end date.';
		if (choice.start > todayUtc(now)) return 'The start date is in the future.';
		return null;
	}
	return null;
}

export function historyRequest(choice: HistoryChoice, now: number = Date.now()): HistoryRequest {
	if (choice.mode === 'years') return { mode: 'days', days: Math.round(choice.years * 365.25) };
	if (choice.mode === 'range') {
		const end = choice.end >= todayUtc(now) ? new Date(now).toISOString().replace(/\.\d{3}Z$/, 'Z') : `${choice.end}T23:59:59Z`;
		return { mode: 'range', start: `${choice.start}T00:00:00Z`, end };
	}
	return { mode: 'all' };
}

export function historyText(choice: HistoryChoice): string {
	if (choice.mode === 'years') return `last ${choice.years} year${choice.years === 1 ? '' : 's'}`;
	if (choice.mode === 'range') return `${choice.start} → ${choice.end} UTC`;
	return 'all available history';
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
