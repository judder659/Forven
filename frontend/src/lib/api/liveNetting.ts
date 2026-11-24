// Live netting engine (forven/live_netting.py): many live strategies share one
// exchange position per coin. Status, the switch, its settings and the
// operator's tools for a coin whose books and exchange position differ.
import { fetchApi } from './core';

export const NETTING_CONFIRM_PHRASE = 'NETTING';

export interface NettingTrade {
	trade_id: string;
	strategy_id: string;
	side: number;
	size: number;
	entry_price: number | null;
	stop_price: number | null;
	target_price: number | null;
	opened_at: string | null;
}

export interface NettingDisasterStop {
	oid: string | null;
	direction: string;
	size: number;
	price: number;
	placed_at: string | null;
	error: string | null;
}

export interface NettingMismatch {
	book_units: number;
	exchange_units: number;
	seen: number;
	first_seen: string | null;
	confirmed: boolean;
	note: string | null;
}

export interface NettingCoin {
	coin: string;
	book_units: number;
	exchange_units: number | null;
	long_units: number;
	short_units: number;
	disaster_stop: NettingDisasterStop | null;
	mismatch: NettingMismatch | null;
	trades: NettingTrade[];
}

export interface NettingSlots {
	slots: number;
	slots_wanted: number | null;
	slots_capital_cap: number | null;
	capital_bound: boolean | null;
	slot_usd: number;
	override?: boolean;
	slots_calculated?: number;
	cohort_size: number | null;
	smallest_size_pct: number | null;
	equity_usd: number | null;
	computed_at: string | null;
	in_use: number;
}

export interface NettingStatus {
	enabled: boolean;
	active: boolean;
	books_on: boolean;
	settings: {
		live_netting_enabled: boolean;
		live_netting_slot_coverage_pct: number;
		live_netting_disaster_gap_pct: number;
		live_netting_slots_override: number;
	};
	coins: NettingCoin[];
	slots: NettingSlots | null;
	last_sweep_at?: string | null;
	error?: string;
}

export type NettingResolveAction = 'trade_to_books' | 'close_books';

export function getLiveNetting(): Promise<NettingStatus> {
	return fetchApi<NettingStatus>('/api/live/netting');
}

export function setLiveNetting(enabled: boolean, confirm?: string): Promise<NettingStatus> {
	return fetchApi<NettingStatus>('/api/live/netting', {
		method: 'POST',
		body: JSON.stringify({ enabled, confirm: confirm ?? null })
	});
}

export function updateLiveNettingSettings(changes: {
	slots_override?: number;
	slot_coverage_pct?: number;
	disaster_gap_pct?: number;
}): Promise<NettingStatus> {
	return fetchApi<NettingStatus>('/api/live/netting/settings', {
		method: 'POST',
		body: JSON.stringify(changes)
	});
}

export function checkLiveNetting(): Promise<NettingStatus> {
	return fetchApi<NettingStatus>('/api/live/netting/check', { method: 'POST' });
}

export function resolveLiveNetting(coin: string, action: NettingResolveAction): Promise<NettingStatus> {
	return fetchApi<NettingStatus>('/api/live/netting/resolve', {
		method: 'POST',
		body: JSON.stringify({ coin, action })
	});
}
