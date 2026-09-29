/**
 * Execution check: why a paper or live strategy can or cannot open new entries,
 * and the guarded fixes (GET/POST /api/strategies/{id}/execution-check).
 *
 * - Live: accept a completed backtest of the current settings as the live baseline.
 *   Research gates are not re-run; the operator's reason is recorded.
 * - Paper or live: restore the settings the accepted evidence was validated with.
 * - Paper without verified evidence: only a gauntlet run re-verifies.
 */
import { fetchApi } from './core';

export type ExecutionCheckKind =
	| 'ok'
	| 'not_executing'
	| 'unverified'
	| 'params_changed'
	| 'engine_changed'
	| 'source_changed'
	| 'config_changed'
	| 'unavailable'
	| 'other';

export interface ExecutionChange {
	/** Dotted path, e.g. "execution_profile.leverage". */
	key: string;
	validated: unknown;
	current: unknown;
	kind: 'added' | 'removed' | 'changed';
}

export interface ExecutionCandidate {
	result_id: string;
	created_at: string | null;
	start_date: string | null;
	end_date: string | null;
	trades: number;
	/** Engine *_pct metrics are fractions (0.05 = 5%). */
	total_return_pct: number | null;
	max_drawdown_pct: number | null;
	win_rate: number | null;
	profit_factor: number | null;
	sharpe: number | null;
	leverage: number | null;
}

export interface ExecutionAccepted {
	verified: boolean;
	result_id: string | null;
	/** "existing_live_execution" for an accepted live baseline, "promotion" otherwise. */
	scope: string;
	leverage: number | null;
	params: Record<string, unknown>;
	reason: string | null;
}

export interface ExecutionCheck {
	strategy_id: string;
	stage: string;
	executing: boolean;
	executable: boolean | null;
	kind: ExecutionCheckKind;
	reason: string | null;
	accepted: ExecutionAccepted | null;
	current_params?: Record<string, unknown>;
	changes: ExecutionChange[];
	candidates: ExecutionCandidate[];
	actions: { accept_backtest: boolean; restore: boolean; gauntlet: boolean };
	open_position_update?: unknown;
}

const path = (strategyId: string) => `/strategies/${encodeURIComponent(strategyId)}/execution-check`;

export function getExecutionCheck(strategyId: string): Promise<ExecutionCheck> {
	return fetchApi(path(strategyId));
}

export function acceptExecutionBacktest(strategyId: string, resultId: string, reason: string): Promise<ExecutionCheck> {
	return fetchApi(`${path(strategyId)}/accept`, { method: 'POST', body: JSON.stringify({ result_id: resultId, reason }) });
}

export function restoreValidatedParams(strategyId: string, reason: string): Promise<ExecutionCheck> {
	return fetchApi(`${path(strategyId)}/restore`, { method: 'POST', body: JSON.stringify({ reason }) });
}
