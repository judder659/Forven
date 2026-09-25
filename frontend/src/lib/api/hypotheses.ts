import { fetchApi } from './core';

export type HypothesisLane = 'exploration' | 'exploitation' | 'benchmarking';
export type HypothesisManagerView = 'active' | 'archived' | 'trash' | 'graduated';
export type HypothesisQuality = 'placeholder' | 'researching' | 'enriched' | 'productive';
/** Stored proof axis (source of truth for the verdict pipeline). */
export type HypothesisStatus = 'proposed' | 'researching' | 'proven' | 'disproven';
/** Derived user-facing lifecycle stage (design-spec vocabulary). */
export type CrucibleStatus = 'proposed' | 'testing' | 'viable' | 'expanded' | 'failed';
export type CrucibleProtectionStatus = 'unprotected' | 'protected' | 'contested';
/** Where the idea came from. */
export type CrucibleOrigin = 'agent' | 'harvested' | 'operator';

export interface VerdictSignals {
	rolling_window_setting: number;
	rolling_window_size: number;
	hit_rate: number;
	diversity_cells: number;
	dead_children?: number;
	hit_rate_threshold: number;
	min_diversity_cells: number;
	/** Diversity bar after the proportional cap to the thesis's declared scope. */
	effective_min_diversity_cells?: number;
	mathematical_verdict: 'researching' | 'proven' | 'disproven';
}

export interface VerdictMemo {
	verdict: 'researching' | 'proven' | 'disproven';
	rationale?: string;
	evidence_summary?: string;
	next_step_suggestions?: string[];
	garbage_signal?: boolean;
	decided_after_n_strategies?: number;
	signals?: VerdictSignals | null;
	/** Ruling on the source's claimed edge (the "was the podcaster right?" verdict). */
	claim_verdict?: 'confirmed' | 'partially_confirmed' | 'disproven' | 'unverified' | 'no_claim';
	claim_assessment?: string;
}

export interface StrategyLatestResult {
	result_id: string;
	created_at?: string | null;
	sharpe?: number | null;
	total_return_pct?: number | null;
	total_trades?: number | null;
	win_rate?: number | null;
	max_drawdown_pct?: number | null;
}

export interface HypothesisActiveTask {
	task_id: number;
	display_id?: string | null;
	type: string;
	status: 'pending' | 'running' | 'paused_manual' | string;
	title: string;
	origin_mode?: string | null;
	created_at?: string | null;
}

export interface HypothesisMutationResponse {
	hypothesis: HypothesisSummary;
}

export interface HypothesisBulkMutationResponse {
	hypotheses: HypothesisSummary[];
}

export interface CrucibleWorkState { state: string; reason?: string | null; task_id: number; task_display_id?: string | null; }

export interface HypothesisSummary {
	work_state?: CrucibleWorkState | null;
	id: string;
	display_id?: string | null;
	title: string;
	lane: HypothesisLane | string;
	/** Derived: where the idea came from (agent-invented / harvested / operator). */
	origin: CrucibleOrigin | string;
	source_type: string;
	origin_agent_id?: string | null;
	origin_role?: string | null;
	origin_model?: string | null;
	origin_model_id?: string | null;
	status: HypothesisStatus | string;
	/** Derived user-facing lifecycle stage; the stored proof axis is `status`. */
	crucible_status: CrucibleStatus | string;
	manager_state: HypothesisManagerView;
	protection_status: CrucibleProtectionStatus | string;
	protected_at?: string | null;
	contested_at?: string | null;
	initial_viability_evidence_id?: string | null;
	novelty_score: number;
	target_assets: string[];
	target_timeframes: string[];
	strategy_count: number;
	best_result?: string | null;
	best_outcome?: StrategyLatestResult | null;
	open_data_gap_count: number;
	quality: HypothesisQuality;
	active_task?: HypothesisActiveTask | null;
	source_tags: string[];
	archived_at?: string | null;
	deleted_at?: string | null;
	restored_at?: string | null;
	created_at?: string | null;
	updated_at?: string | null;
	verdict_memo?: VerdictMemo | null;
	verdict_memo_at?: string | null;
	verdict_memo_by?: string | null;
	graduated_at?: string | null;
	next_revisit_at?: string | null;
	last_revisited_at?: string | null;
	revisit_count?: number | null;
}

export interface HypothesisArtifact {
	id: string;
	hypothesis_id: string;
	source_type: string;
	source_title: string;
	source_ref: string;
	claimed_edge: string;
	implementation_summary: string;
	adaptation_notes?: string | null;
	caveats?: string | null;
	created_at?: string | null;
	cached_content?: string | null;
	cached_content_hash?: string | null;
	cached_at?: string | null;
	content_bytes?: number | null;
}

export interface DataGapRequester {
	id: string;
	display_id?: string | null;
	title: string;
}

export interface DataGapSummary {
	id: string;
	title: string;
	category: string;
	missing_dataset: string;
	missing_fields: string[];
	why_it_matters?: string | null;
	request_count: number;
	priority_score: number;
	created_at?: string | null;
	updated_at?: string | null;
	/** Hypotheses (crucibles) that requested this gap, directly or via a strategy. */
	requesting_hypotheses?: DataGapRequester[];
	requesting_hypothesis_ids?: string[];
}

export interface HypothesisDetailStrategy {
	thesis_revision?: 'current' | 'older' | 'unverified';
	id: string;
	name: string;
	type?: string | null;
	symbol?: string | null;
	timeframe?: string | null;
	stage: string;
	status?: string | null;
	/** Latest gauntlet (forge) workflow status: passed / failed_gate / running / pending / blocked_*. */
	gauntlet_status?: string | null;
	owner?: string | null;
	latest_result?: StrategyLatestResult | null;
	updated_at?: string | null;
	canonical?: boolean | number | null;
	parent_strategy_id?: string | null;
}

export interface AgentActivityEntry {
	task_id: number;
	display_id?: string | null;
	type: string;
	status: string;
	title: string;
	origin_mode?: string | null;
	created_at?: string | null;
	feedback?: string | null;
	decision?: string | null;
	audit_events: Array<Record<string, unknown>>;
}

export interface HypothesisResearchTask {
	task_id: number;
	display_id?: string | null;
	type: string;
	status: 'pending' | 'running' | 'paused_manual' | string;
	title: string;
	origin_mode?: string | null;
	created_at?: string | null;
}

export interface HypothesisDetailResponse {
	work_state?: CrucibleWorkState | null;
	hypothesis: HypothesisSummary & {
		market_thesis: string;
		mechanism: string;
		why_now?: string | null;
		operator_notes?: string | null;
		source_tags: string[];
		verdict_signals?: VerdictSignals | null;
	};
	strategies: HypothesisDetailStrategy[];
	artifacts: HypothesisArtifact[];
	data_gaps: DataGapSummary[];
	research_task?: HypothesisResearchTask | null;
	agent_activity: AgentActivityEntry[];
}

export interface HypothesisListResponse {
	hypotheses: HypothesisSummary[];
	/** Total rows in the bucket after filtering, before pagination. Present only when limit/offset is sent. */
	total?: number;
	limit?: number | null;
	offset?: number;
}

export async function getHypotheses(params?: {
	view?: HypothesisManagerView;
	lane?: string;
	status?: string;
	source_type?: string;
	search?: string;
	sort?: string;
	quality?: HypothesisQuality | '';
	include_disproven?: boolean;
	/** Page size. Omit to fetch the full filtered list (legacy behaviour). */
	limit?: number;
	/** Row offset for server-side pagination. */
	offset?: number;
}): Promise<HypothesisListResponse> {
	const qs = new URLSearchParams();
	if (params?.view) qs.set('view', params.view);
	if (params?.lane) qs.set('lane', params.lane);
	if (params?.status) qs.set('status', params.status);
	if (params?.source_type) qs.set('source_type', params.source_type);
	if (params?.search) qs.set('search', params.search);
	if (params?.sort) qs.set('sort', params.sort);
	if (params?.quality) qs.set('quality', params.quality);
	if (params?.include_disproven) qs.set('include_disproven', 'true');
	if (typeof params?.limit === 'number') qs.set('limit', String(params.limit));
	if (typeof params?.offset === 'number' && params.offset > 0) qs.set('offset', String(params.offset));
	const query = qs.toString();
	return fetchApi(`/hypotheses${query ? `?${query}` : ''}`);
}

export type HypothesisCounts = Record<HypothesisManagerView, number>;

export async function getHypothesisCounts(): Promise<{ counts: HypothesisCounts }> {
	return fetchApi('/hypotheses/counts');
}

export async function getHypothesisDetail(
	id: string,
	options?: { includeContent?: boolean },
): Promise<HypothesisDetailResponse> {
	const qs = options?.includeContent ? '?include=content' : '';
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}${qs}`);
}

export interface UpdateHypothesisRequest {
	title?: string;
	market_thesis?: string;
	mechanism?: string;
	why_now?: string;
	target_assets?: string[];
	target_timeframes?: string[];
	novelty_score?: number;
	operator_notes?: string;
}

export async function updateHypothesis(
	id: string,
	body: UpdateHypothesisRequest,
): Promise<HypothesisMutationResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/update`, {
		method: 'POST',
		body: JSON.stringify(body),
	});
}

export interface RetriggerResearchResponse {
	ok: boolean;
	task: { task_id: number | null; display_id?: string | null; [key: string]: unknown } | null;
	already_running: boolean;
}

export async function retriggerHypothesisResearch(id: string): Promise<RetriggerResearchResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/research`, {
		method: 'POST',
	});
}

export interface GenerateStrategiesResponse {
	ok: boolean;
	task: { task_id: number | null; display_id?: string | null; [key: string]: unknown } | null;
	already_running: boolean;
}

export async function generateHypothesisStrategies(
	id: string,
	opts: { force?: boolean } = {},
): Promise<GenerateStrategiesResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/generate-strategies`, {
		method: 'POST',
		body: JSON.stringify({ force: Boolean(opts.force) }),
	});
}

export async function getRankedDataGaps(limit = 20): Promise<{ items: DataGapSummary[] }> {
	return fetchApi(`/data-gaps?limit=${limit}`);
}

export async function archiveHypothesis(id: string): Promise<HypothesisMutationResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/archive`, {
		method: 'POST',
	});
}

export async function trashHypothesis(id: string): Promise<HypothesisMutationResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/trash`, {
		method: 'POST',
	});
}

export async function restoreHypothesis(id: string): Promise<HypothesisMutationResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/restore`, {
		method: 'POST',
	});
}

export async function bulkArchiveHypotheses(ids: string[]): Promise<HypothesisBulkMutationResponse> {
	return fetchApi('/hypotheses/bulk/archive', {
		method: 'POST',
		body: JSON.stringify({ ids }),
	});
}

export async function bulkTrashHypotheses(ids: string[]): Promise<HypothesisBulkMutationResponse> {
	return fetchApi('/hypotheses/bulk/trash', {
		method: 'POST',
		body: JSON.stringify({ ids }),
	});
}

export async function bulkRestoreHypotheses(ids: string[]): Promise<HypothesisBulkMutationResponse> {
	return fetchApi('/hypotheses/bulk/restore', {
		method: 'POST',
		body: JSON.stringify({ ids }),
	});
}

export type UrlSourceType = 'youtube' | 'reddit' | 'github' | 'blog' | 'forum' | 'podcast';

export interface UrlPreviewSuccess {
	ok: true;
	source_type: UrlSourceType;
	url: string;
	title: string;
	content_preview: string;
	content_bytes: number;
	preview_truncated: boolean;
}

export interface UrlPreviewFailure {
	ok: false;
	source_type?: UrlSourceType | null;
	error_code: string;
	error: string;
}

export type UrlPreviewResponse = UrlPreviewSuccess | UrlPreviewFailure;

export async function previewHypothesisFromUrl(url: string): Promise<UrlPreviewResponse> {
	return fetchApi('/hypotheses/preview_url', {
		method: 'POST',
		body: JSON.stringify({ url }),
	});
}

export interface CreateFromUrlRequest {
	request_id?: string;
	url: string;
	title?: string;
	market_thesis?: string;
	mechanism?: string;
	claimed_edge?: string;
}

export interface CreateFromUrlSuccess {
	ok: true;
	hypothesis: HypothesisSummary & {
		market_thesis: string;
		mechanism: string;
	};
	task?: { task_id: number | null; display_id?: string | null; error?: string } | null;
	research_deferred?: boolean;
}

export interface CreateFromUrlFailure {
	ok: false;
	source_type?: UrlSourceType | null;
	error_code: string;
	error: string;
}

export type CreateFromUrlResponse = CreateFromUrlSuccess | CreateFromUrlFailure;

export async function createHypothesisFromUrl(body: CreateFromUrlRequest): Promise<CreateFromUrlResponse> {
	return fetchApi('/hypotheses/from_url', {
		method: 'POST',
		body: JSON.stringify(body),
	});
}

export interface CreateFromUrlsRequest {
	request_id?: string;
	urls: string[];
	title?: string;
	market_thesis?: string;
	mechanism?: string;
	claimed_edge?: string;
}

/** Per-URL outcome echoed back by the combine endpoint. */
export interface UrlSourceResult {
	url: string;
	ok: boolean;
	source_type?: UrlSourceType | null;
	title?: string;
	content_bytes?: number;
	error_code?: string;
	error?: string;
}

export interface CreateFromUrlsSuccess {
	ok: true;
	hypothesis: HypothesisSummary & {
		market_thesis: string;
		mechanism: string;
	};
	task?: { task_id: number | null; error?: string } | null;
	sources: UrlSourceResult[];
	research_deferred?: boolean;
}

export interface CreateFromUrlsFailure {
	ok: false;
	error_code: string;
	error: string;
	sources?: UrlSourceResult[];
}

export type CreateFromUrlsResponse = CreateFromUrlsSuccess | CreateFromUrlsFailure;

/** Combine several source URLs into a single crucible (one per all sources). */
export async function createHypothesisFromUrls(
	body: CreateFromUrlsRequest,
): Promise<CreateFromUrlsResponse> {
	return fetchApi('/hypotheses/from_urls', {
		method: 'POST',
		body: JSON.stringify(body),
	});
}

export interface CreateManualRequest {
	request_id?: string;
	title: string;
	market_thesis: string;
	mechanism: string;
	why_now?: string;
	target_assets?: string[];
	target_timeframes?: string[];
	novelty_score?: number;
	claimed_edge?: string;
	operator_notes?: string;
}

export interface CreateManualResponse {
	ok: true;
	hypothesis: HypothesisSummary & {
		market_thesis: string;
		mechanism: string;
	};
	task?: { task_id: number | null; display_id?: string | null; error?: string } | null;
	research_deferred?: boolean;
}

export async function createHypothesisManual(body: CreateManualRequest): Promise<CreateManualResponse> {
	return fetchApi('/hypotheses/manual', {
		method: 'POST',
		body: JSON.stringify(body),
	});
}

export async function reopenHypothesis(
	id: string,
	rationale?: string,
): Promise<HypothesisMutationResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/reopen`, {
		method: 'POST',
		body: JSON.stringify({ rationale }),
	});
}

export interface TriggerVerdictResponse {
	ok: boolean;
	hypothesis?: HypothesisSummary;
	error_code?: string;
	raw?: string;
}

export async function triggerVerdict(id: string): Promise<TriggerVerdictResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/verdict`, { method: 'POST' });
}

export interface ForceRevisitResponse {
	hypothesis_id: string;
	manager_state: 'active';
	status: 'researching';
	next_revisit_at: string;
}

export async function forceRevisitHypothesis(id: string): Promise<ForceRevisitResponse> {
	return fetchApi(`/hypotheses/${encodeURIComponent(id)}/revisit`, { method: 'POST' });
}

export interface EvidenceCleanupResponse {
	disproven_count?: number;
	would_disprove_count?: number;
	ids: string[];
}

export async function runEvidenceCleanup(dryRun = false): Promise<EvidenceCleanupResponse> {
	const qs = dryRun ? '?dry_run=true' : '';
	return fetchApi(`/hypotheses/cleanup/evidence${qs}`, { method: 'POST' });
}

export interface TriageBatchResponse {
	processed_count: number;
	processed_ids: string[];
	errors: Array<{ id: string; error_code?: string }>;
}

export async function runTriageBatch(batchSize = 10): Promise<TriageBatchResponse> {
	return fetchApi(`/hypotheses/cleanup/triage/start?batch_size=${batchSize}`, {
		method: 'POST',
	});
}

export interface DiscoverCruciblesResponse {
	created: boolean;
	reason?: string;
	task_id?: number | null;
	mode?: 'operator_approves' | 'autonomous' | string;
}

/** Operator-triggered harvest: dispatch one external-source crucible-discovery task. */
export async function discoverCrucibles(): Promise<DiscoverCruciblesResponse> {
	return fetchApi('/hypotheses/discover', { method: 'POST' });
}

// ── CRUX-1 allocator overview (Crucibles page intelligence strip) ───────────

export interface AllocatorCrucible {
	id: string;
	display_id: string;
	title: string;
	status: string;
	protection_status?: string;
	created_at?: string | null;
	family: string;
	family_survival_rate: number;
	score: number;
	children: number;
	gauntlet_children: number;
	survivor_children: number;
	positive_children: number;
	fruitless_develops: number;
	failed_develops: number;
	last_child_created_at?: string | null;
}

export interface AllocatorOverview {
	budget: { daily: number; used_today: number; remaining: number };
	short_quota: {
		target_pct: number;
		develops_today: number;
		directed_today: number;
		share_pct: number;
	};
	data_quota?: {
		target_pct: number;
		develops_today: number;
		directed_today: number;
		share_pct: number;
	};
	pool: { total: number; by_status: Record<string, number>; with_survivors: number };
	/** Research yield over the last `days`: how many candidates actually got tested and how far. */
	yield?: {
		days: number;
		candidates: number;
		untestable: number;
		no_trades: number;
		reached_gauntlet: number;
		reached_paper: number;
		disproven: number;
		parked: number;
	};
	crucibles: AllocatorCrucible[];
}

/** CRUX-1: daily develop budget, short quota, and the value-ranked active pool. */
export async function getAllocatorOverview(limit = 40): Promise<AllocatorOverview> {
	return fetchApi(`/hypotheses/allocator?limit=${limit}`);
}

export function getHypothesisReadiness(id: string): Promise<import('./strategyCreator').IdeaReadiness> {
 return fetchApi(`/hypotheses/${encodeURIComponent(id)}/readiness`);
}
