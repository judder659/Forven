// Live probation: a strategy trading live at reduced size until its live
// trades confirm the backtest (forven/probation.py). Every automatic move has a
// manual twin here.
import { ApiError, fetchApi } from './core';

export const PROBATION_CONFIRM_PHRASE = 'PROBATION';
export const GO_LIVE_CONFIRM_PHRASE = 'GO LIVE';

export type ProbationState = 'active' | 'paused' | 'demoting' | 'graduated' | 'demoted';

export interface ProbationCheck {
	key: string;
	label: string;
	ok: boolean | null;
	detail: string;
	bypassable?: boolean;
}

export interface ProbationTrigger {
	key: string;
	label: string;
	detail: string;
}

export interface ProbationLadder {
	start_size_pct: number;
	step_trades: number;
	step_size_pct: number;
	graduate_trades: number;
}

export interface ProbationNextStep {
	at_trades: number;
	size_pct: number;
}

export interface ProbationAutomation {
	auto_entry: boolean;
	auto_graduate: boolean;
	auto_demote: boolean;
}

export interface ProbationHistoryEvent {
	event: string;
	at?: string;
	actor?: string;
	[key: string]: unknown;
}

export interface ProbationRecord {
	strategy_id: string;
	state: ProbationState;
	size_pct: number;
	size_locked: boolean;
	entered_at: string | null;
	entered_by: string | null;
	bypassed: string[];
	history: ProbationHistoryEvent[];
	demotion_count: number;
	last_demoted_at: string | null;
	status_reason: string | null;
	in_probation: boolean;
}

export interface ProbationLiveRecord {
	live_trades: number;
	live_return_pct: number;
	live_drawdown_pct: number;
	drawdown_limit_pct: number | null;
	losing_streak: number;
	losing_streak_limit: number | null;
	backtest_trades: number;
	sequential_state: string | null;
	mismatched_trades: number;
	triggers: ProbationTrigger[];
}

export interface ProbationEntryReport {
	eligible: boolean;
	checks: ProbationCheck[];
	failing: ProbationCheck[];
	slow: boolean;
	trades_per_month: number | null;
	paper_days: number | null;
	confirmation: { matched: number; mismatched: number; closed: number };
	notes: string[];
}

export interface ProbationSkip {
	at?: string;
	asset?: string;
	reason?: string;
}

export interface ProbationStatus {
	strategy_id: string;
	stage: string;
	record: ProbationRecord | null;
	in_probation: boolean;
	automation: ProbationAutomation;
	ladder: ProbationLadder;
	skips: ProbationSkip[];
	live?: ProbationLiveRecord;
	open_positions?: number;
	next_step?: ProbationNextStep | null;
	ladder_size_pct?: number;
	graduation?: { eligible: boolean; checks: ProbationCheck[]; failing: ProbationCheck[] };
	entry?: ProbationEntryReport;
}

export interface ProbationOverviewRow {
	strategy_id: string;
	display_id: string;
	name: string;
	symbol: string;
	timeframe: string;
	state: ProbationState;
	size_pct: number;
	size_locked: boolean;
	entered_at: string | null;
	live_trades: number;
	live_return_pct: number;
	triggers: ProbationTrigger[];
	next_step: ProbationNextStep | null;
}

export interface ProbationReadyRow {
	strategy_id: string;
	display_id: string;
	name: string;
	symbol: string;
	timeframe: string;
	eligible: boolean;
	failing: { key: string; label: string; detail: string }[];
	confirmation: { matched: number; mismatched: number; closed: number };
}

export interface ProbationOverview {
	strategies: ProbationOverviewRow[];
	ladder: ProbationLadder;
	max_strategies: number;
	automation: ProbationAutomation;
	ready: { checked_at: string | null; strategies: ProbationReadyRow[] };
}

export type ProbationBadges = Record<string, { state: ProbationState; size_pct: number }>;

/** The refusal the backend sends: a message plus the checks that failed. */
export interface ProbationRefusal {
	message: string;
	checks: ProbationCheck[];
	needs_confirm: string | null;
}

export function probationRefusal(error: unknown): ProbationRefusal | null {
	if (!(error instanceof ApiError)) return null;
	const detail = (error.payload as { detail?: unknown } | null)?.detail;
	if (detail && typeof detail === 'object') {
		const d = detail as Partial<ProbationRefusal>;
		return {
			message: String(d.message ?? error.message),
			checks: Array.isArray(d.checks) ? d.checks : [],
			needs_confirm: d.needs_confirm ?? null,
		};
	}
	return { message: error.message, checks: [], needs_confirm: null };
}

const post = <T>(path: string, body: unknown) =>
	fetchApi<T>(path, { method: 'POST', body: JSON.stringify(body) });

const strategyPath = (id: string, action = '') =>
	`/probation/strategies/${encodeURIComponent(id)}${action ? `/${action}` : ''}`;

export const getProbationOverview = () => fetchApi<ProbationOverview>('/probation');
export const getProbationBadges = () => fetchApi<ProbationBadges>('/probation/badges');
export const getProbationStatus = (id: string) => fetchApi<ProbationStatus>(strategyPath(id));

export const startProbation = (id: string, body: { confirm: string; bypass?: boolean; reason?: string }) =>
	post<ProbationStatus>(strategyPath(id, 'start'), body);

/** null follows the ladder again; 100 graduates (needs GO LIVE). */
export const setProbationSize = (id: string, sizePct: number | null, confirm?: string) =>
	post<ProbationStatus>(strategyPath(id, 'size'), { size_pct: sizePct, confirm: confirm ?? null });

export const graduateProbation = (id: string, body: { confirm: string; bypass?: boolean }) =>
	post<ProbationStatus>(strategyPath(id, 'graduate'), body);

export const pauseProbation = (id: string, paused: boolean) =>
	post<ProbationStatus>(strategyPath(id, 'pause'), { paused });

export const demoteProbation = (id: string, reason = '') =>
	post<ProbationStatus>(strategyPath(id, 'demote'), { reason });

export const getProbationAutomation = () => fetchApi<ProbationAutomation>('/probation/automation');

export const setProbationAutomation = (changes: Partial<ProbationAutomation> & { confirm?: string | null }) =>
	post<ProbationAutomation>('/probation/automation', changes);

export const runProbationCheck = () => post<Record<string, unknown>>('/probation/check', {});
