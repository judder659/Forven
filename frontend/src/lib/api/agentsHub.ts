/**
 * Typed client for the Agents page: the fleet summary, what the agents
 * produced, recent runs, one agent's working context, and the run actions.
 */
import { fetchApi } from './core';
import type { TaskContainer } from './lifecycle';

export type FleetWindow = '24h' | '7d';
export type AgentState = 'running' | 'queued' | 'paused' | 'idle';
export type RunOutcome = 'ok' | 'failed' | 'blocked' | 'stopped' | 'open';

export interface FleetRunRef {
	id: number;
	display_id: string;
	title: string;
	type: string | null;
	strategy_id: string | null;
}

export interface FleetRunning extends FleetRunRef {
	started_at: string | null;
	timeout_seconds?: number;
	detail?: string;
}

export interface FleetBucket {
	ok: number;
	failed: number;
	blocked: number;
}

export interface FleetWindowStats {
	runs: number;
	ok: number;
	failed: number;
	blocked: number;
	stopped: number;
	success_rate: number | null;
	median_seconds: number | null;
	tokens: number;
	buckets: FleetBucket[];
	types: Record<string, number>;
}

export interface FleetSpend {
	today: number;
	d7: number;
	d30: number;
	tokens_today: number;
	tokens_d7: number;
}

export interface FleetLastRun extends FleetRunRef {
	status: string;
	completed_at: string | null;
	error: string | null;
}

export interface FleetAgent {
	id: string;
	name: string;
	role: string;
	model: string;
	model_id: string;
	enabled: boolean;
	visibility: string;
	is_core: boolean;
	state: AgentState;
	running: FleetRunning[];
	pending: number;
	oldest_pending_at: string | null;
	paused_manual: number;
	blocked: number;
	failed_open: number;
	window: FleetWindowStats;
	spend: FleetSpend;
	last: FleetLastRun | null;
}

export interface FleetProblemGroup {
	kind: 'blocked' | 'failed';
	agent_id: string;
	reason: string;
	example: string;
	count: number;
	task_ids: number[];
	resumable_ids: number[];
	latest_at: string | null;
	sample: FleetRunRef;
}

export interface FleetSchedulerProblem {
	id: string;
	name: string;
	command: string;
	error: string;
	last_run_at: string | null;
	running_since: string | null;
	next_run_at: string | null;
}

export interface FleetStuckRun extends FleetRunning {
	agent_id: string;
}

export interface FleetTotals {
	agents: number;
	running: number;
	pending: number;
	paused: number;
	blocked: number;
	blocked_resumable: number;
	failed_open: number;
	runs: number;
	ok: number;
	failed: number;
	tokens: number;
	spend_today: number;
	spend_d7: number;
	spend_d30: number;
}

export interface AgentFleet {
	generated_at: string;
	window: FleetWindow;
	window_start: string;
	bucket_seconds: number;
	agents: FleetAgent[];
	totals: FleetTotals;
	attention: {
		stuck: FleetStuckRun[];
		blocked: FleetProblemGroup[];
		failed: FleetProblemGroup[];
		paused_backlog: Array<{ agent_id: string; pending: number; oldest_pending_at: string | null }>;
		scheduler: FleetSchedulerProblem[];
		brain_failed: Array<FleetRunRef & { error: string; completed_at: string | null; detail?: string }>;
	};
	autonomy: { mode: string } | null;
}

export interface YieldStrategy {
	id: string;
	stage: string;
	agent_id: string;
	model: string | null;
	created_at: string | null;
	notes: string | null;
	status_reason: string | null;
	gauntlet_seen: boolean;
}

export interface AgentYield {
	days: number;
	since: string;
	strategies: YieldStrategy[];
	ideas: Record<string, number>;
	spend: Record<string, number>;
}

export interface ActivityRun extends FleetRunRef {
	agent_id: string;
	status: string;
	outcome: RunOutcome;
	started_at: string | null;
	completed_at: string | null;
	seconds: number | null;
	tokens: number;
	cost_usd: number | null;
	model: string;
	error: string | null;
	summary: string | null;
}

export interface AgentWorkspaceRun {
	id: number;
	display_id: string | null;
	type: string | null;
	title: string | null;
	status: string;
	provider: string | null;
	model_id: string | null;
	started_at: string | null;
	completed_at: string | null;
	created_at: string | null;
	total_tokens: number | null;
	cost_usd: number | null;
	strategy_id: string | null;
	error: string | null;
}

export interface AgentWorkspace {
	memory: string;
	memory_today: string;
	memory_day: string;
	documents: { soul: string; agents: string; role: string };
	logs: Array<{ id: number; level: string; source: string; message: string; created_at: string }>;
	runs: AgentWorkspaceRun[];
}

export function getAgentFleet(window: FleetWindow = '24h'): Promise<AgentFleet> {
	return fetchApi(`/agents/fleet?window=${window}`);
}

export function getAgentYield(days = 7): Promise<AgentYield> {
	return fetchApi(`/agents/yield?days=${days}`);
}

export function getAgentActivity(limit = 40): Promise<ActivityRun[]> {
	return fetchApi(`/agents/activity?limit=${limit}`);
}

/** The agent row (Discord token flag, instructions) for the settings tab. */
export function getAgentRow(agentId: string): Promise<{ id: string; name: string; has_discord_token?: boolean }> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}`);
}

/** Memory, documents, activity log and recent runs for one agent. */
export function getAgentWorkspace(agentId: string): Promise<AgentWorkspace> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}/terminal`);
}

/** Pause (false) or resume (true) an agent. The task worker only claims runs for enabled agents. */
export function setAgentEnabled(agentId: string, enabled: boolean): Promise<Record<string, unknown>> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}`, {
		method: 'PATCH',
		body: JSON.stringify({ enabled }),
	});
}

export function renameAgent(agentId: string, name: string): Promise<Record<string, unknown>> {
	return fetchApi(`/agents/${encodeURIComponent(agentId)}`, {
		method: 'PATCH',
		body: JSON.stringify({ name }),
	});
}

/** Clear a failed, blocked or cancelled agent run from the attention list. */
export function dismissAgentRun(taskId: number, note?: string): Promise<{ ok: boolean }> {
	return fetchApi(`/agent-tasks/${taskId}/dismiss`, {
		method: 'POST',
		body: JSON.stringify({ source: 'agent_tasks', note }),
	});
}

/** Queue a blocked run again from its saved checkpoint (409 when there is none). */
export function resumeAgentRun(taskId: number): Promise<{ ok: boolean; status: string }> {
	return fetchApi(`/agent-tasks/${taskId}/resume`, { method: 'POST' });
}

export interface RunQuery {
	statuses?: string[];
	agentId?: string;
	limit?: number;
}

/** Agent runs, newest first; dismissed runs are left out by the server. */
export async function listAgentRuns(query: RunQuery = {}): Promise<TaskContainer[]> {
	const params = new URLSearchParams();
	params.set('limit', String(query.limit ?? 400));
	if (query.statuses?.length) params.set('status', query.statuses.join(','));
	if (query.agentId) params.set('agent_id', query.agentId);
	const payload = await fetchApi<{ tasks: TaskContainer[] }>(`/pipeline/task-containers?${params.toString()}`);
	return Array.isArray(payload?.tasks) ? payload.tasks : [];
}

export interface BulkResult {
	ok: number;
	failed: Array<{ id: number; error: string }>;
}

/** Run one action over many runs, a few at a time, and report every failure. */
export async function forEachRun(
	ids: number[],
	action: (id: number) => Promise<unknown>,
	concurrency = 4,
): Promise<BulkResult> {
	const queue = [...ids];
	const result: BulkResult = { ok: 0, failed: [] };
	async function worker() {
		for (let id = queue.shift(); id !== undefined; id = queue.shift()) {
			try {
				await action(id);
				result.ok += 1;
			} catch (error) {
				result.failed.push({ id, error: error instanceof Error ? error.message : String(error) });
			}
		}
	}
	await Promise.all(Array.from({ length: Math.max(1, Math.min(concurrency, ids.length)) }, worker));
	return result;
}
