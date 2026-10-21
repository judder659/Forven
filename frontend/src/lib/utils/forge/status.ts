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
	| 'probation'
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
	probation: {
		key: 'probation',
		label: 'Probation',
		short: 'probation',
		tone: 'caution',
		rank: 6,
		help: 'Trading live at reduced size until its live trades confirm the backtest.',
	},
	live: {
		key: 'live',
		label: 'Live',
		short: 'trading',
		tone: 'ok',
		rank: 7,
		help: 'Trading real capital at full size.',
	},
	unknown: {
		key: 'unknown',
		label: 'Not assessed',
		short: 'unassessed',
		tone: 'idle',
		rank: 8,
		help: 'The pipeline explainer has not reported on this strategy yet.',
	},
};

/**
 * The words for one strategy, specific to where it is. Paper strategies are
 * waiting on the probation entry checks, so they read as the check that holds
 * them ("Confirming 1/3", "Paper losing"); probation strategies show their size
 * and live-trade count. Every other stage uses the plain status words.
 */
export function entryStatusMeta(
	entry: PipelineExplainStrategy | null | undefined,
	fallbackStatus: string | null | undefined = null,
): StatusMeta {
	const base = statusMeta(entry?.status ?? fallbackStatus);
	if (!entry || base.key === 'awaiting_operator') return base;
	const probation = entry.evidence?.probation;
	if (entry.pipeline_stage === 'probation' || base.key === 'probation') {
		const size = probation?.size_pct;
		const trades = probation?.live_trades;
		const need = probation?.graduate_trades;
		const sizeText = typeof size === 'number' ? ` ${Math.round(size)}%` : '';
		const tradesText = typeof trades === 'number' && typeof need === 'number' ? ` · ${trades}/${need} live trades` : '';
		const paused = probation?.state === 'paused' ? ' · paused' : probation?.state === 'demoting' ? ' · demoting' : '';
		return { ...STATUS_META.probation, label: `Probation${sizeText}${tradesText}${paused}` };
	}
	if (entry.stage !== 'paper' || !probation) return base;
	if (base.key === 'ready') {
		return { ...base, label: 'Ready for probation', short: 'ready', help: 'Every probation check passes. Start probation on the strategy page.' };
	}
	const failing = new Set(probation.failing ?? []);
	const pick = (label: string, short: string, tone: Tone, help: string): StatusMeta => ({ ...base, label, short, tone, help });
	if (failing.has('edge_covers_costs') && base.key === 'blocked_merit') {
		return pick('Edge too thin', 'edge thin', 'fail', 'The backtest edge per trade does not cover trading costs with room to spare.');
	}
	if (failing.has('held_back') && base.key === 'blocked_merit') {
		return pick('Failed held-back data', 'held back', 'fail', 'It lost on the data kept back from research.');
	}
	if (failing.has('paper_breakeven')) {
		return pick('Paper losing', 'losing', 'caution', 'Still paper trading. Probation needs paper at breakeven or better.');
	}
	if (failing.has('paper_confirmation')) {
		const days = probation.slow && probation.need_days ? `, ${Math.floor(Number(probation.paper_days ?? 0))}/${probation.need_days}d` : '';
		return pick(
			`Confirming ${probation.matched}/${probation.need_trades}${days}`,
			'confirming',
			'wait',
			'Paper trades so far that matched what the backtest would have done. Probation needs this many.',
		);
	}
	if (failing.has('cooldown')) {
		return pick('Cooling down', 'cooling', 'wait', 'It was demoted from probation recently and waits out the cooldown first.');
	}
	if (failing.has('capacity') || failing.has('live_admission')) {
		return pick('No room live', 'no room', 'caution', 'Probation is full, or the live account has no room for it right now.');
	}
	return base;
}

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

function progressItem(key: string, label: string, current: number, threshold: number, unit: string): ProgressItem {
	const met = current >= threshold;
	return { key, label, current, threshold, unit, met, fraction: threshold > 0 ? Math.max(0, Math.min(1, current / threshold)) : 1 };
}

/** Paper progress toward probation: matched paper trades (and days, for slow strategies). */
export function paperProgress(entry: PipelineExplainStrategy | null | undefined): ProgressItem[] {
	if (!entry || entry.stage !== 'paper') return [];
	const p = entry.evidence?.probation;
	if (!p || !Number.isFinite(Number(p.need_trades)) || Number(p.need_trades) <= 0) return [];
	const out = [progressItem('paper_confirmation', 'Matched trades', Number(p.matched ?? 0), Number(p.need_trades), 'trades')];
	if (p.slow && Number(p.need_days) > 0) {
		out.push(progressItem('paper_days', 'Days', Math.floor(Number(p.paper_days ?? 0)), Number(p.need_days), 'days'));
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
	probation: 'Probation',
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
