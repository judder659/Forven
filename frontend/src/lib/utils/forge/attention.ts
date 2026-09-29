// "Needs you": the short list of things only the operator can move. Everything
// else in the factory resolves on its own (evidence accumulates, validation runs
// finish, sweeps archive failures), so it stays off this list on purpose.

import type { PipelineExplainStrategy } from '$lib/api/lifecycle';
import type { LiveFleet } from '$lib/api/dashboard';
import type { NowWorkingRow } from '$lib/api/forven';
import type { HealthStatusResponse } from '$lib/api/types';
import type { ManagerRow } from '$lib/utils/strategy';
import { approvalTypeLabel, cleanReason, stageLabel, type Tone } from './status';

export type AttentionKind = 'approval' | 'live' | 'blocked' | 'stalled' | 'recovery' | 'health';

export interface AttentionItem {
	key: string;
	kind: AttentionKind;
	tone: Tone;
	strategyId: string | null;
	/** Friendly name; several strategies can share one, so `meta` carries the id. */
	title: string;
	meta: string;
	detail: string;
	actionLabel: string;
	href: string;
}

const KIND_RANK: Record<AttentionKind, number> = {
	approval: 0,
	live: 1,
	health: 2,
	stalled: 3,
	recovery: 4,
	blocked: 5,
};

export function containerHref(id: string): string {
	return `/lab/strategy/${encodeURIComponent(id)}?returnTo=${encodeURIComponent('/lab')}`;
}

export interface AttentionInput {
	/** Only strategies the Forge lists; the explainer also reports non-pipeline scratch rows. */
	activeIds: Set<string>;
	explain: PipelineExplainStrategy[];
	fleet: LiveFleet | null;
	nowWorking: NowWorkingRow[];
	rows: ManagerRow[];
	health: HealthStatusResponse | null;
	nameOf: (id: string) => string;
}

export function buildAttention(input: AttentionInput): AttentionItem[] {
	const items: AttentionItem[] = [];
	const { activeIds, nameOf } = input;

	for (const entry of input.explain) {
		if (!activeIds.has(entry.id)) continue;
		const approval = entry.pending_approval;
		if (approval) {
			items.push({
				key: `approval:${approval.id}`,
				kind: 'approval',
				tone: 'caution',
				strategyId: entry.id,
				title: nameOf(entry.id),
				meta: `${entry.id} · ${stageLabel(entry.stage)}`,
				detail: `${capitalize(approvalTypeLabel(approval.approval_type))}${approval.requested_status ? ` → ${stageLabel(approval.requested_status).toLowerCase()}` : ''}`,
				actionLabel: `Review #${approval.id}`,
				href: `/approval?approval_id=${encodeURIComponent(String(approval.id))}`,
			});
			continue;
		}
		if (entry.status === 'blocked_merit') {
			items.push({
				key: `blocked:${entry.id}`,
				kind: 'blocked',
				tone: 'fail',
				strategyId: entry.id,
				title: nameOf(entry.id),
				meta: `${entry.id} · ${stageLabel(entry.stage)}`,
				detail: cleanReason(entry.gate_reason || entry.blockers?.[0]?.reason) || 'Missed a quality bar',
				actionLabel: 'Decide',
				href: containerHref(entry.id),
			});
		} else if (entry.status === 'slot_contention') {
			items.push({
				key: `slot:${entry.id}`,
				kind: 'blocked',
				tone: 'caution',
				strategyId: entry.id,
				title: nameOf(entry.id),
				meta: `${entry.id} · ${stageLabel(entry.stage)}`,
				detail: cleanReason(entry.gate_reason) || 'Another strategy holds its slot',
				actionLabel: 'Decide',
				href: containerHref(entry.id),
			});
		}
	}

	for (const live of input.fleet?.strategies ?? []) {
		if (live.state !== 'stale' && live.state !== 'blocked') continue;
		const refused = live.blocked_entries;
		const detail =
			live.state === 'stale'
				? `Live scanner has not evaluated it recently${live.open_trade_ids?.length ? ' — with a position open' : ''}`
				: `${refused?.count ?? 0} live entr${refused?.count === 1 ? 'y' : 'ies'} refused in ${refused?.window_days ?? 30}d — ${cleanReason(refused?.top_reason || refused?.last_reason) || 'see the strategy page'}`;
		items.push({
			key: `live:${live.strategy_id}`,
			kind: 'live',
			tone: live.state === 'stale' ? 'fail' : 'caution',
			strategyId: live.strategy_id,
			title: nameOf(live.strategy_id),
			meta: `${live.strategy_id} · Live`,
			detail,
			actionLabel: 'Open',
			href: containerHref(live.strategy_id),
		});
	}

	for (const work of input.nowWorking) {
		if (!work.current_task?.stalled) continue;
		const hasStrategy = !String(work.strategy_id).startsWith('task-');
		items.push({
			key: `stalled:${work.strategy_id}:${work.current_task.type}`,
			kind: 'stalled',
			tone: 'fail',
			strategyId: hasStrategy ? work.strategy_id : null,
			title: hasStrategy ? nameOf(work.strategy_id) : work.name,
			meta: hasStrategy ? work.strategy_id : 'Engine',
			detail: `${taskLabel(work.current_task.type)} stalled`,
			actionLabel: 'Inspect',
			href: hasStrategy ? containerHref(work.strategy_id) : '/pipeline?tab=pipeline',
		});
	}

	for (const row of input.rows) {
		if ((row.recovery_status ?? '').toLowerCase() !== 'exhausted') continue;
		items.push({
			key: `recovery:${row.id}`,
			kind: 'recovery',
			tone: 'fail',
			strategyId: row.id,
			title: nameOf(row.id),
			meta: row.id,
			detail: row.recovery_last_error ? `Recovery gave up — ${cleanReason(row.recovery_last_error)}` : 'Automatic recovery gave up',
			actionLabel: 'Open',
			href: containerHref(row.id),
		});
	}

	for (const check of input.health?.data_checks ?? []) {
		if (check.passed || check.severity !== 'critical') continue;
		items.push({
			key: `health:${check.name}`,
			kind: 'health',
			tone: 'fail',
			strategyId: null,
			title: friendlyName(check.name),
			meta: 'System',
			detail: check.detail,
			actionLabel: 'Open ops',
			href: '/ops',
		});
	}

	return items.sort((a, b) => KIND_RANK[a.kind] - KIND_RANK[b.kind]);
}

function capitalize(text: string): string {
	return text.charAt(0).toUpperCase() + text.slice(1);
}

export function friendlyName(name: string): string {
	return name
		.replace(/^bot:/, '')
		.replace(/_/g, ' ')
		.replace(/\b\w/g, (c) => c.toUpperCase());
}

const TASK_LABELS: Record<string, string> = {
	'forven-testing-cycle': 'Testing cycle',
	'forven-gauntlet': 'Gauntlet run',
	walk_forward: 'Walk-forward',
	monte_carlo: 'Monte Carlo',
	parameter_jitter: 'Parameter jitter',
	cost_stress: 'Cost stress',
	regime_split: 'Regime split',
	optimization: 'Optimization',
	backtest: 'Backtest',
};

/** "forven-testing-cycle" → "Testing cycle". */
export function taskLabel(type: string | null | undefined): string {
	const key = String(type ?? '').trim();
	if (!key) return 'Task';
	if (TASK_LABELS[key]) return TASK_LABELS[key];
	const words = key.replace(/^forven[-_]/, '').replace(/[-_]+/g, ' ').trim();
	return words.charAt(0).toUpperCase() + words.slice(1);
}
