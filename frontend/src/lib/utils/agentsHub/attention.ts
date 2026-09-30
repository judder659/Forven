// "Needs you": everything on the agent side that will not resolve without the
// operator, most urgent first. Built from the fleet payload (server-side
// aggregates, so counts are totals, not a page of rows) plus provider health.

import type { AgentFleet, FleetProblemGroup } from '$lib/api/agentsHub';
import type { AgentProviderWarning, ProviderRuntimeHealth } from '$lib/api';
import type { Tone } from '$lib/utils/forge/status';
import { ago, elapsed } from '$lib/utils/forge/time';
import { fmtSeconds, plural } from './format';
import { providerLabel } from './agents';

export type AttentionActionKind = 'resume' | 'dismiss' | 'resume-agent' | 'link';

export interface AttentionAction {
	kind: AttentionActionKind;
	label: string;
	ids?: number[];
	agentId?: string;
	href?: string;
}

export interface AttentionItem {
	key: string;
	tone: Tone;
	title: string;
	detail: string;
	meta: string;
	agentId: string | null;
	actions: AttentionAction[];
}

/** Failure groups shown one by one; the rest fold into a single "more" row. */
export const FAILED_GROUPS_SHOWN = 4;

// Jobs whose failure touches live or paper trading directly.
const TRADING_COMMANDS = new Set([
	'scanner',
	'scanner-signal',
	'reconcile-sweep',
	'phantom-sweep',
	'decay-kill-switch',
	'propr-mirror',
	'slippage-monitor',
	'funding-history-reconcile',
	'portfolio-allocation',
	'capital-slot-dedupe',
	'regime-gate-mtm',
	'basket-funding-carry',
]);

export interface ProviderHealthInput {
	runtime: ProviderRuntimeHealth[];
	warnings: AgentProviderWarning[];
}

function runsHref(agentId: string, statuses: string): string {
	const params = new URLSearchParams({ tab: 'tasks', status: statuses });
	if (agentId) params.set('agent', agentId);
	return `/agents?${params.toString()}`;
}

function epochToMs(value: string | number | null | undefined): number | string | null {
	if (typeof value === 'number') return value < 1e12 ? value * 1000 : value;
	return value ?? null;
}

function problemItem(group: FleetProblemGroup, name: string, now: number): AttentionItem {
	const blocked = group.kind === 'blocked';
	const actions: AttentionAction[] = [];
	if (blocked && group.resumable_ids.length > 0) {
		actions.push({ kind: 'resume', label: `Resume ${group.resumable_ids.length}`, ids: group.resumable_ids });
	}
	actions.push({ kind: 'dismiss', label: group.count === 1 ? 'Dismiss' : `Dismiss ${group.count}`, ids: group.task_ids });
	actions.push({ kind: 'link', label: 'View', href: runsHref(group.agent_id, blocked ? 'blocked' : 'failed') });
	return {
		key: `${group.kind}:${group.agent_id}:${group.reason}`,
		tone: blocked ? 'caution' : 'fail',
		title: `${name} · ${plural(group.count, blocked ? 'blocked run' : 'failed run')}`,
		detail: group.example || group.reason,
		meta: group.latest_at ? `latest ${ago(group.latest_at, now)}` : '',
		agentId: group.agent_id,
		actions,
	};
}

export function buildAttention(
	fleet: AgentFleet,
	providers: ProviderHealthInput | null,
	now: number = Date.now(),
	failedShown: number = FAILED_GROUPS_SHOWN,
): AttentionItem[] {
	const names = new Map(fleet.agents.map((agent) => [agent.id, agent.name]));
	const nameOf = (id: string) => names.get(id) ?? (id || 'Unassigned');
	const items: AttentionItem[] = [];

	for (const run of fleet.attention.stuck) {
		items.push({
			key: `stuck:${run.id}`,
			tone: 'fail',
			title: `${nameOf(run.agent_id)} · run past its time limit`,
			detail: `“${run.title}” has run ${elapsed(run.started_at, now)}; the limit is ${fmtSeconds(run.timeout_seconds ?? null)}, so it may be hung.`,
			meta: run.display_id,
			agentId: run.agent_id,
			actions: [{ kind: 'link', label: 'Open run', href: `/tasks/${encodeURIComponent(run.display_id)}?returnTo=${encodeURIComponent('/agents')}` }],
		});
	}

	const jobs = [...fleet.attention.scheduler].sort(
		(a, b) => Number(TRADING_COMMANDS.has(b.command)) - Number(TRADING_COMMANDS.has(a.command)),
	);
	for (const job of jobs) {
		const trading = TRADING_COMMANDS.has(job.command);
		items.push({
			key: `job:${job.id}`,
			tone: 'fail',
			title: `${job.name} failed${trading ? ' · trading job' : ''}`,
			detail: job.error,
			meta: job.last_run_at ? `last run ${ago(job.last_run_at, now)}` : '',
			agentId: null,
			actions: [{ kind: 'link', label: 'Schedules', href: '/agents?tab=schedules' }],
		});
	}

	for (const health of providers?.runtime ?? []) {
		if (health.state !== 'down' && health.state !== 'degraded') continue;
		const down = health.state === 'down';
		items.push({
			key: `provider:${health.provider}`,
			tone: down ? 'fail' : 'caution',
			title: `${providerLabel(health.provider)} is ${down ? 'down' : 'degraded'}${health.kind ? ` · ${String(health.kind).replace(/_/g, ' ')}` : ''}`,
			detail: [health.message, health.fallback_to ? `Falling back to ${health.fallback_to}.` : 'No fallback: calls on it fail.']
				.filter(Boolean)
				.join(' '),
			meta: health.last_event_at ? `last call ${ago(epochToMs(health.last_event_at), now)}` : '',
			agentId: null,
			actions: [{ kind: 'link', label: 'Health', href: '/agents?tab=health' }],
		});
	}

	for (const group of fleet.attention.blocked) items.push(problemItem(group, nameOf(group.agent_id), now));

	for (const backlog of fleet.attention.paused_backlog) {
		items.push({
			key: `paused:${backlog.agent_id}`,
			tone: 'caution',
			title: `${nameOf(backlog.agent_id)} is paused`,
			detail: `${plural(backlog.pending, 'run')} waiting${backlog.oldest_pending_at ? ` since ${ago(backlog.oldest_pending_at, now)}` : ''}. They run when you resume the agent.`,
			meta: '',
			agentId: backlog.agent_id,
			actions: [{ kind: 'resume-agent', label: 'Resume agent', agentId: backlog.agent_id }],
		});
	}

	for (const warning of providers?.warnings ?? []) {
		items.push({
			key: `pinned:${warning.agent_id}:${warning.provider}`,
			tone: 'caution',
			title: `${nameOf(warning.agent_id)} uses ${providerLabel(warning.provider)}, which has no credentials`,
			detail: warning.fallback ? `Its runs fall back to ${warning.fallback}.` : 'There is no fallback, so its runs fail.',
			meta: '',
			agentId: warning.agent_id,
			actions: [{ kind: 'link', label: 'Routing', href: '/agents?tab=routing' }],
		});
	}

	const failed = fleet.attention.failed;
	for (const group of failed.slice(0, failedShown)) items.push(problemItem(group, nameOf(group.agent_id), now));
	if (failed.length > failedShown) {
		const rest = failed.slice(failedShown);
		const runs = rest.reduce((sum, group) => sum + group.count, 0);
		items.push({
			key: 'failed:more',
			tone: 'fail',
			title: `${plural(runs, 'more failed run')} across ${plural(rest.length, 'other error')}`,
			detail: 'Older or rarer failures, grouped by error on the Runs tab.',
			meta: '',
			agentId: null,
			actions: [
				{ kind: 'dismiss', label: `Dismiss ${runs}`, ids: rest.flatMap((group) => group.task_ids) },
				{ kind: 'link', label: 'View', href: runsHref('', 'failed') },
			],
		});
	}

	const brainFailed = fleet.attention.brain_failed;
	if (brainFailed.length > 0) {
		const latest = brainFailed[brainFailed.length - 1];
		items.push({
			key: 'brain:failed',
			tone: 'caution',
			title: `Brain · ${plural(brainFailed.length, 'failed cycle')} in this window`,
			detail: latest.error || 'The cycle failed without a message.',
			meta: latest.completed_at ? `latest ${ago(latest.completed_at, now)}` : '',
			agentId: 'brain',
			actions: [{ kind: 'link', label: 'Brain', href: '/brain' }],
		});
	}

	return items;
}
