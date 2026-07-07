/**
 * Brain API client (P1-T13) — backs the /brain page (P1-T14..T17),
 * the Settings auxiliary picker (P1-T18), and the strategy decision
 * widget (P1-T19).
 *
 * Contract is fixed by ``forven/routers/brain.py``. Keep these
 * interfaces in lockstep with the backend payload shapes.
 */

import { fetchApi } from './core';

// --------------------------------------------------------------------------- //
// Overview                                                                    //
// --------------------------------------------------------------------------- //

export interface BrainOverviewMemory {
	body: string;
	updated_at: string | null;
	updated_by: string | null;
	char_count: number;
	cap: number;
}

export interface BrainOverviewStats {
	activity_count: number;
	active_tasks: number;
	recent_tasks: number;
	failed_tasks: number;
	blocked_tasks: number;
	pending_approvals: number;
	decisions: number;
	recalls: number;
}

export interface BrainAttentionItem {
	kind: string;
	severity: 'critical' | 'warning' | 'info' | string;
	title: string;
	detail: string;
}

export interface BrainActivityRow {
	id: number;
	level: string;
	source: string | null;
	message: string;
	data: string | null;
	created_at: string;
}

export interface BrainOverviewTask {
	id: number;
	display_id: string | null;
	agent_id: string | null;
	type: string | null;
	title: string | null;
	status: string | null;
	strategy_id: string | null;
	priority: number | null;
	created_at: string | null;
	started_at: string | null;
	completed_at: string | null;
	error: string | null;
}

export interface BrainRepeatedFailure {
	type: string | null;
	count: number;
}

export interface BrainOverview {
	memory: BrainOverviewMemory;
	stats: BrainOverviewStats;
	attention: BrainAttentionItem[];
	activity: BrainActivityRow[];
	active_tasks: BrainOverviewTask[];
	recent_tasks: BrainOverviewTask[];
	repeated_failures: BrainRepeatedFailure[];
}

export async function getBrainOverview(): Promise<BrainOverview> {
	return fetchApi<BrainOverview>('/brain/overview');
}

// --------------------------------------------------------------------------- //
// Memory                                                                      //
// --------------------------------------------------------------------------- //

export interface BrainMemoryState {
	body: string;
	updated_at: string | null;
	updated_by: string | null;
	char_count: number;
	cap: number;
}

export type BrainMemoryMutationType = 'replace' | 'add' | 'remove';

export interface BrainMemoryHistoryRow {
	id: number;
	mutation_type: BrainMemoryMutationType;
	before_excerpt: string | null;
	after_excerpt: string | null;
	mutated_at: string;
	mutated_by: string | null;
}

export interface BrainMemoryHistoryResponse {
	history: BrainMemoryHistoryRow[];
	cap: number;
}

export interface BrainMemoryCapErrorDetail {
	error: 'memory_cap_exceeded';
	current_len: number;
	attempted_len: number;
	cap: number;
}

export async function getBrainMemory(): Promise<BrainMemoryState> {
	return fetchApi<BrainMemoryState>('/brain/memory');
}

export async function putBrainMemory(
	body: string,
	options?: { mutatedBy?: string }
): Promise<BrainMemoryState> {
	return fetchApi<BrainMemoryState>('/brain/memory', {
		method: 'PUT',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({
			body,
			mutation_type: 'replace',
			mutated_by: options?.mutatedBy ?? 'operator'
		})
	});
}

export async function getBrainMemoryHistory(
	limit = 20
): Promise<BrainMemoryHistoryResponse> {
	const params = new URLSearchParams({ limit: String(limit) });
	return fetchApi<BrainMemoryHistoryResponse>(
		`/brain/memory/history?${params.toString()}`
	);
}

// --------------------------------------------------------------------------- //
// Decisions                                                                   //
// --------------------------------------------------------------------------- //

export type BrainDecisionOutcome = 'success' | 'failure' | 'mixed' | string;

export interface BrainDecisionRow {
	id: number;
	cycle_id: string | null;
	situation_summary: string | null;
	decision_json: string | null;
	decision: unknown | null;
	action_taken: string | null;
	outcome_observed: BrainDecisionOutcome | null;
	outcome_at: string | null;
	prompt_hash: string | null;
	created_at: string;
}

export interface BrainDecisionsListResponse {
	items: BrainDecisionRow[];
	total: number;
	limit: number;
	offset: number;
}

export interface BrainDecisionLinkedTask {
	id: number;
	display_id: string | null;
	agent_id: string | null;
	type: string | null;
	title: string | null;
	status: string | null;
	strategy_id: string | null;
	provider: string | null;
	model_id: string | null;
	cost_usd: number | null;
	created_at: string | null;
	completed_at: string | null;
}

export interface BrainDecisionDetail extends BrainDecisionRow {
	linked_tasks: BrainDecisionLinkedTask[];
}

export interface BrainDecisionsListQuery {
	cycleId?: string;
	actionType?: string;
	strategyId?: string;
	outcome?: BrainDecisionOutcome;
	limit?: number;
	offset?: number;
}

export async function getBrainDecisions(
	query: BrainDecisionsListQuery = {}
): Promise<BrainDecisionsListResponse> {
	const params = new URLSearchParams();
	if (query.cycleId) params.set('cycle_id', query.cycleId);
	if (query.actionType) params.set('action_type', query.actionType);
	if (query.strategyId) params.set('strategy_id', query.strategyId);
	if (query.outcome) params.set('outcome', query.outcome);
	if (query.limit !== undefined) params.set('limit', String(query.limit));
	if (query.offset !== undefined) params.set('offset', String(query.offset));
	const qs = params.toString();
	return fetchApi<BrainDecisionsListResponse>(
		qs ? `/brain/decisions?${qs}` : '/brain/decisions'
	);
}

export async function getBrainDecision(
	decisionId: number
): Promise<BrainDecisionDetail> {
	return fetchApi<BrainDecisionDetail>(`/brain/decisions/${decisionId}`);
}

// --------------------------------------------------------------------------- //
// Recall                                                                      //
// --------------------------------------------------------------------------- //

export type BrainRecallScope = 'all' | 'decisions' | 'tasks';
export type BrainRecallSource = 'brain_decisions' | 'agent_tasks';

export interface BrainRecallHit {
	source: BrainRecallSource;
	id: number;
	score: number;
	rerank_score?: number;
	snippet: string | null;
	situation: string | null;
	outcome: string | null;
	created_at: string | null;
	deep_link_url: string;
}

export interface BrainRecallSuccess {
	ok: true;
	query: string;
	scope: BrainRecallScope;
	limit: number;
	summary: string;
	hits: BrainRecallHit[];
	aux_model: string | null;
	latency_ms: number;
}

export interface BrainRecallFailure {
	ok: false;
	error: 'recall_failed' | string;
	detail?: string;
	summary: '';
	hits: [];
	aux_model: null;
	latency_ms: 0;
}

export type BrainRecallResponse = BrainRecallSuccess | BrainRecallFailure;

export async function recallSimilarSituation(
	q: string,
	options: { scope?: BrainRecallScope; limit?: number } = {}
): Promise<BrainRecallResponse> {
	const params = new URLSearchParams({ q });
	if (options.scope) params.set('scope', options.scope);
	if (options.limit !== undefined) params.set('limit', String(options.limit));
	return fetchApi<BrainRecallResponse>(`/brain/recall?${params.toString()}`);
}

// --------------------------------------------------------------------------- //
// Auxiliary model routing                                                     //
// --------------------------------------------------------------------------- //

export type BrainAuxiliaryTaskKind =
	| 'recall'
	| 'skill_extraction'
	| 'approval';

export interface BrainAuxiliaryEntry {
	provider: string | null;
	model_id: string | null;
	base_url: string | null;
	api_key: string | null;
}

export interface BrainAuxiliaryResponse {
	auxiliary: Record<BrainAuxiliaryTaskKind, BrainAuxiliaryEntry>;
	task_kinds: BrainAuxiliaryTaskKind[];
}

export async function getBrainAuxiliary(): Promise<BrainAuxiliaryResponse> {
	return fetchApi<BrainAuxiliaryResponse>('/brain/auxiliary');
}

export async function updateBrainAuxiliary(
	auxiliary: Partial<Record<BrainAuxiliaryTaskKind, BrainAuxiliaryEntry>>
): Promise<BrainAuxiliaryResponse> {
	return fetchApi<BrainAuxiliaryResponse>('/brain/auxiliary', {
		method: 'PUT',
		body: JSON.stringify({ auxiliary })
	});
}

