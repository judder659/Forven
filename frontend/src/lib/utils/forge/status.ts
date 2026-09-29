// What the pipeline explainer says about each active strategy, turned into the
// words and tones the Forge shows. The explainer (GET /lifecycle/pipeline/explain)
// dry-runs the real gates, so these labels describe the same verdict the
// promotion path would reach; nothing here re-derives a gate.

import type { PipelineExplainBlocker, PipelineExplainStrategy } from '$lib/api/lifecycle';

export type Tone = 'ok' | 'info' | 'wait' | 'caution' | 'fail' | 'idle';

export type ForgeStatusKey =
	| 'awaiting_operator'
	| 'blocked_merit'
	| 'slot_contention'
	| 'ready'
	| 'in_flight'
	| 'waiting_evidence'
	| 'live'
	| 'unknown';

export interface StatusMeta {
	key: ForgeStatusKey;
	label: string;
	/** One-word form for tight spots (the pipeline rail). */
	short: string;
	tone: Tone;
	/** Lower sorts first: what needs the operator floats to the top. */
	rank: number;
	help: string;
}

export const STATUS_META: Record<ForgeStatusKey, StatusMeta> = {
	awaiting_operator: {
		key: 'awaiting_operator',
		label: 'Needs your review',
		short: 'review',
		tone: 'caution',
		rank: 0,
		help: 'An approval is waiting on you before this strategy can move.',
	},
	blocked_merit: {
		key: 'blocked_merit',
		label: 'Blocked on merit',
		short: 'blocked',
		tone: 'fail',
		rank: 1,
		help: 'It was measured and missed a quality bar. Waiting will not fix it: revise, re-optimize or archive.',
	},
	slot_contention: {
		key: 'slot_contention',
		label: 'Slot taken',
		short: 'slot taken',
		tone: 'caution',
		rank: 2,
		help: 'Another strategy holds the slot it needs; a dethrone decides which one keeps it.',
	},
	ready: {
		key: 'ready',
		label: 'Ready to promote',
		short: 'ready',
		tone: 'ok',
		rank: 3,
		help: 'Every gate for the next stage passes.',
	},
	in_flight: {
		key: 'in_flight',
		label: 'Validating',
		short: 'validating',
		tone: 'info',
		rank: 4,
		help: 'A validation run is in flight; its verdict lands on its own.',
	},
	waiting_evidence: {
		key: 'waiting_evidence',
		label: 'Gathering evidence',
		short: 'gathering',
		tone: 'wait',
		rank: 5,
		help: 'Nothing has failed. The gate needs more evidence (paper days, trades or fresh validation) first.',
	},
	live: {
		key: 'live',
		label: 'Live',
		short: 'trading',
		tone: 'ok',
		rank: 6,
		help: 'Trading real capital on the graduated schedule.',
	},
	unknown: {
		key: 'unknown',
		label: 'Not assessed',
		short: 'unassessed',
		tone: 'idle',
		rank: 7,
		help: 'The pipeline explainer has not reported on this strategy yet.',
	},
};

export function statusMeta(status: string | null | undefined): StatusMeta {
	const key = String(status ?? '').trim().toLowerCase() as ForgeStatusKey;
	return STATUS_META[key] ?? STATUS_META.unknown;
}

/** Text + dot classes per tone, in the strategy page's palette. */
export const TONE_TEXT: Record<Tone, string> = {
	ok: 'text-[#3cc48f]',
	info: 'text-[#7fb2ff]',
	wait: 'text-sc-ink2',
	caution: 'text-[#e7b24a]',
	fail: 'text-[#f2956f]',
	idle: 'text-sc-ink3',
};

export const TONE_DOT: Record<Tone, string> = {
	ok: 'bg-[#3cc48f]',
	info: 'bg-[#7fb2ff]',
	wait: 'border border-sc-ink2 bg-transparent',
	caution: 'bg-[#e7b24a]',
	fail: 'bg-[#e5574f]',
	idle: 'bg-sc-ink4',
};

export const TONE_BAR: Record<Tone, string> = {
	ok: 'bg-[#3cc48f]',
	info: 'bg-[#7fb2ff]',
	wait: 'bg-sc-ink3',
	caution: 'bg-[#e7b24a]',
	fail: 'bg-[#e5574f]',
	idle: 'bg-sc-line2',
};

export const TONE_PILL: Record<Tone, string> = {
	ok: 'border-[#3cc48f]/40 bg-[#3cc48f]/10 text-[#3cc48f]',
	info: 'border-[#7fb2ff]/40 bg-[#7fb2ff]/10 text-[#7fb2ff]',
	wait: 'border-sc-line2 bg-sc-panel2 text-sc-ink2',
	caution: 'border-[#e7b24a]/40 bg-[#e7b24a]/10 text-[#e7b24a]',
	fail: 'border-[#e5574f]/45 bg-[#e5574f]/10 text-[#f2956f]',
	idle: 'border-sc-line2 text-sc-ink3',
};

const REASON_PREFIXES = [
	/^live gate:\s*/i,
	/^paper gate:\s*/i,
	/^quick screen reject:\s*/i,
	/^quick-screen reject:\s*/i,
	/^gauntlet gate:\s*/i,
	/^gauntlet failed_gate:\s*/i,
];

/** The first clause of a gate reason, without the gate prefix. */
export function cleanReason(text: string | null | undefined, { firstClause = true } = {}): string {
	let out = String(text ?? '').trim();
	if (!out) return '';
	for (const prefix of REASON_PREFIXES) out = out.replace(prefix, '');
	// Live-path refusals read "BLOCKED BTC live — <why>"; the why is the useful part.
	out = out.replace(/^BLOCKED\s+\S+\s+live\s*(?:—|--|-)\s*/i, '');
	out = out.replace(/IS->OOS/g, 'IS→OOS').replace(/\s+/g, ' ');
	if (firstClause) {
		const cut = out.search(/ — | -- |\. (?=[A-Z])/);
		if (cut > 12) out = out.slice(0, cut);
	}
	out = out.replace(/[.;:,\s]+$/, '');
	return out.charAt(0).toUpperCase() + out.slice(1);
}

/** "strategy_dethrone_recommendation" → "dethrone recommendation". */
export function approvalTypeLabel(type: string | null | undefined): string {
	return String(type ?? '')
		.replace(/^strategy_/, '')
		.replace(/_/g, ' ')
		.trim() || 'approval';
}

export interface StatusLine {
	/** One line for a table cell. */
	text: string;
	/** The full wording, for a tooltip or the drawer. */
	full: string;
}

/** The one sentence that says why a strategy is where it is. */
export function statusLine(entry: PipelineExplainStrategy | null | undefined): StatusLine {
	if (!entry) return { text: '', full: '' };
	const approval = entry.pending_approval;
	if (entry.status === 'awaiting_operator' && approval) {
		const text = `Approval #${approval.id} · ${approvalTypeLabel(approval.approval_type)}`;
		return { text, full: approval.reason ? `${text} — ${approval.reason}` : text };
	}
	const reason = entry.gate_reason || entry.blockers?.[0]?.reason || '';
	if (reason) return { text: cleanReason(reason), full: cleanReason(reason, { firstClause: false }) };
	const next = entry.next_action?.label || entry.next_transition?.trigger || '';
	return { text: cleanReason(next), full: next };
}

export interface ProgressItem {
	key: string;
	label: string;
	current: number;
	threshold: number;
	unit: string;
	met: boolean;
	/** 0–1 toward the threshold (capped). */
	fraction: number;
}

const PROGRESS_LABELS: Record<string, string> = {
	paper_duration: 'Days',
	paper_trades: 'Trades',
};

function toProgress(key: string, extra: unknown): ProgressItem | null {
	if (!extra || typeof extra !== 'object') return null;
	const rec = extra as Record<string, unknown>;
	const current = Number(rec.current);
	const threshold = Number(rec.threshold);
	if (!Number.isFinite(current) || !Number.isFinite(threshold) || threshold <= 0) return null;
	const direction = String(rec.direction ?? 'gte');
	const met = direction === 'gt' ? current > threshold : current >= threshold;
	return {
		key,
		label: PROGRESS_LABELS[key] ?? key,
		current,
		threshold,
		unit: String(rec.unit ?? ''),
		met,
		fraction: Math.max(0, Math.min(1, current / threshold)),
	};
}

/** Paper warm-up progress toward the paper→live gate: days and trades. */
export function paperProgress(entry: PipelineExplainStrategy | null | undefined): ProgressItem[] {
	if (!entry || entry.stage !== 'paper') return [];
	const out: ProgressItem[] = [];
	for (const key of ['paper_duration', 'paper_trades'] as const) {
		const step = (entry.readiness_steps ?? []).find((s) => s?.name === key);
		const item = toProgress(key, step?.extra) ?? toProgress(key, entry.evidence?.paper?.[key]);
		if (item) out.push(item);
	}
	return out;
}

export const BLOCKER_KIND_LABEL: Record<string, string> = {
	evidence: 'Evidence',
	merit: 'Merit',
	contention: 'Slot',
	advisory: 'Advisory',
};

export function blockerTone(blocker: PipelineExplainBlocker): Tone {
	switch (blocker.kind) {
		case 'merit':
			return 'fail';
		case 'contention':
			return 'caution';
		case 'advisory':
			return 'idle';
		default:
			return 'wait';
	}
}

/** Live and paper stage labels the Forge uses everywhere. */
export const STAGE_LABEL: Record<string, string> = {
	quick_screen: 'Quick screen',
	gauntlet: 'Gauntlet',
	paper: 'Paper',
	live_graduated: 'Live',
	archived: 'Archived',
	rejected: 'Rejected',
	backtest_failed: 'Backtest failed',
	// The retired Parked lane; old archival sentences still name it.
	research_only: 'Parked',
};

export function stageLabel(stage: string | null | undefined): string {
	const key = String(stage ?? '').trim().toLowerCase();
	return STAGE_LABEL[key] ?? (key ? key.replace(/_/g, ' ') : '—');
}
