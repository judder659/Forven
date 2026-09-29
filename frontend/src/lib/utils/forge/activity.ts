// Recent pipeline activity from GET /lifecycle/events (newest first). The raw
// feed is ~150–400 rows a day, almost all quick-screen archivals, so runs of the
// same archival (same stage, same cause) collapse into one line. Promotions,
// demotions, revivals and approval requests always stand alone.

import type { LifecycleEvent } from '$lib/api/lifecycle';
import { normalizeStage } from '$lib/utils/strategy';
import { classifyArchive, type CauseKey } from './causes';
import { stageLabel, type Tone } from './status';
import { parseUtc } from './time';
import { PIPELINE_STAGES } from './flow';

export type ActivityKind = 'promoted' | 'archived' | 'demoted' | 'revived' | 'approval';

export interface ActivityItem {
	key: string;
	kind: ActivityKind;
	tone: Tone;
	/** Newest event time in the item. */
	at: string;
	strategyIds: string[];
	fromStage: string | null;
	toStage: string | null;
	causeKey: CauseKey | null;
	title: string;
	detail: string;
	actor: string;
	count: number;
}

export interface HourBucket {
	/** Bucket start (ms since epoch). */
	start: number;
	promoted: number;
	archived: number;
	other: number;
}

/**
 * Pipeline moves per hour over the last `hours`, newest bucket last. `coveredFrom`
 * is the oldest event we actually have: buckets before it are unknown, not quiet.
 */
export function hourlyMoves(events: LifecycleEvent[], now: number, hours = 24): { buckets: HourBucket[]; coveredFrom: number | null } {
	const hourMs = 60 * 60 * 1000;
	const end = Math.floor(now / hourMs) * hourMs + hourMs;
	const start = end - hours * hourMs;
	const buckets: HourBucket[] = Array.from({ length: hours }, (_, i) => ({ start: start + i * hourMs, promoted: 0, archived: 0, other: 0 }));
	let coveredFrom: number | null = null;
	for (const event of events) {
		const ts = parseUtc(event.created_at);
		if (ts === null) continue;
		coveredFrom = coveredFrom === null ? ts : Math.min(coveredFrom, ts);
		if (ts < start || ts >= end) continue;
		const c = classifyEvent(event);
		if (!c) continue;
		const bucket = buckets[Math.floor((ts - start) / hourMs)];
		if (c.kind === 'promoted') bucket.promoted += 1;
		else if (c.kind === 'archived') bucket.archived += 1;
		else bucket.other += 1;
	}
	return { buckets, coveredFrom };
}

const TERMINAL = new Set(['archived', 'rejected', 'backtest_failed']);
/** Archivals further apart than this start a new line even with the same cause. */
const GROUP_WINDOW_MS = 6 * 60 * 60 * 1000;

function rank(stage: string): number {
	return (PIPELINE_STAGES as readonly string[]).indexOf(stage);
}

const ACTORS: Record<string, string> = {
	gauntlet_sweep: 'gauntlet sweep',
	pipeline_sweep: 'hygiene sweep',
	gauntlet_workflow: 'gauntlet workflow',
	gauntlet_evidence_deferral: 'evidence check',
	system: 'system',
	brain: 'Brain',
	policy: 'policy',
	'simulation-agent': 'simulation agent',
	migration: 'migration',
	ui: 'you',
	api: 'you',
	manual: 'you',
	operator: 'you',
};

export function actorLabel(actor: string | null | undefined): string {
	const key = String(actor ?? '').trim();
	return ACTORS[key] ?? key.replace(/[_-]+/g, ' ');
}

function detailsRecord(event: LifecycleEvent): Record<string, unknown> {
	return event.details_json && typeof event.details_json === 'object' ? (event.details_json as Record<string, unknown>) : {};
}

export interface Classified {
	kind: ActivityKind;
	from: string;
	to: string;
	causeKey: CauseKey | null;
	causeText: string;
}

export function classifyEvent(event: LifecycleEvent): Classified | null {
	const fromRaw = String(event.from_state ?? '').trim().toLowerCase();
	const from = fromRaw.startsWith('research') ? 'research_only' : normalizeStage(fromRaw);
	const to = normalizeStage(event.to_state);
	const details = detailsRecord(event);
	if (from === to) {
		if (details.motion === 'operator_approval_required') {
			return { kind: 'approval', from, to: normalizeStage(String(details.requested_stage ?? '')), causeKey: null, causeText: '' };
		}
		return null;
	}
	if (TERMINAL.has(to)) {
		if (from === 'research_only') return null; // retired Parked lane cleanup, not a verdict
		const cause = classifyArchive(event.reason);
		const causeText = cause.detail ? `${cause.label} · ${cause.detail}` : cause.label;
		return { kind: 'archived', from, to, causeKey: cause.key, causeText };
	}
	if (TERMINAL.has(from) || from === 'research_only') return { kind: 'revived', from, to, causeKey: null, causeText: '' };
	const fromRank = rank(from);
	const toRank = rank(to);
	if (fromRank < 0 || toRank < 0) return null;
	return { kind: toRank > fromRank ? 'promoted' : 'demoted', from, to, causeKey: null, causeText: '' };
}

export function buildActivity(
	events: LifecycleEvent[],
	opts: { nameOf: (id: string) => string; limit?: number },
): ActivityItem[] {
	const limit = opts.limit ?? 30;
	const items: ActivityItem[] = [];
	const sorted = [...events].sort((a, b) => (parseUtc(b.created_at) ?? 0) - (parseUtc(a.created_at) ?? 0));
	for (const event of sorted) {
		const c = classifyEvent(event);
		if (!c) continue;
		const last = items[items.length - 1];
		const eventTs = parseUtc(event.created_at) ?? 0;
		if (
			c.kind === 'archived' &&
			last &&
			last.kind === 'archived' &&
			last.fromStage === c.from &&
			last.causeKey === c.causeKey &&
			(parseUtc(last.at) ?? 0) - eventTs <= GROUP_WINDOW_MS
		) {
			last.count += 1;
			if (!last.strategyIds.includes(event.strategy_id)) last.strategyIds.push(event.strategy_id);
			last.title = `${last.count} strategies archived at ${stageLabel(c.from).toLowerCase()}`;
			continue;
		}
		if (items.length >= limit) break;
		const name = opts.nameOf(event.strategy_id);
		let title = '';
		let tone: Tone = 'wait';
		let detail = '';
		switch (c.kind) {
			case 'promoted':
				title = `${name} promoted to ${stageLabel(c.to).toLowerCase()}`;
				detail = `from ${stageLabel(c.from).toLowerCase()}`;
				tone = 'ok';
				break;
			case 'archived':
				title = `${name} archived at ${stageLabel(c.from).toLowerCase()}`;
				detail = c.causeText;
				tone = 'fail';
				break;
			case 'demoted':
				title = `${name} moved back to ${stageLabel(c.to).toLowerCase()}`;
				detail = `from ${stageLabel(c.from).toLowerCase()}`;
				tone = 'caution';
				break;
			case 'revived':
				title = `${name} revived into ${stageLabel(c.to).toLowerCase()}`;
				tone = 'info';
				break;
			case 'approval':
				title = `Approval requested for ${name}`;
				detail = c.to && c.to !== c.from ? `wants to move it to ${stageLabel(c.to).toLowerCase()}` : 'awaiting your decision';
				tone = 'caution';
				break;
		}
		items.push({
			key: `${event.id}`,
			kind: c.kind,
			tone,
			at: event.created_at,
			strategyIds: [event.strategy_id],
			fromStage: c.from,
			toStage: c.to,
			causeKey: c.causeKey,
			title,
			detail,
			actor: actorLabel(event.actor),
			count: 1,
		});
	}
	return items;
}
