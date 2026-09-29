// Stage-to-stage flow over a window, from GET /pipeline/funnel-report?days=N.
// The report counts strategy_events rows by (from_state, to_state). Rules:
//   - self-transitions (a note or re-optimization inside one stage) are not flow;
//   - legacy and retired spellings go through normalizeStage, so the retired
//     Parked lane (research_only) reads as the graveyard;
//   - moves OUT of the graveyard are revivals, counted apart from the forward flow.
// Creation is not an event, so "screened" (strategies the quick screen decided
// on) stands in for new ideas; it is what the factory actually processed.

import { normalizeStage } from '$lib/utils/strategy';

export const PIPELINE_STAGES = ['quick_screen', 'gauntlet', 'paper', 'live_graduated'] as const;
export type PipelineStage = (typeof PIPELINE_STAGES)[number];

const TERMINAL = new Set(['archived', 'rejected', 'backtest_failed']);

export interface StageFlow {
	stage: PipelineStage;
	/** Promoted into this stage from an earlier one. */
	entered: number;
	/** Promoted from this stage to a later one. */
	promoted: number;
	/** Sent from this stage to the graveyard. */
	archived: number;
	/** Moved back to an earlier stage. */
	demoted: number;
	/** promoted / (promoted + archived); null when nothing was decided. */
	passRate: number | null;
}

export interface FlowSummary {
	byStage: Record<PipelineStage, StageFlow>;
	/** Decisions the quick screen made (promoted + archived out of it). */
	screened: number;
	archivedTotal: number;
	revived: number;
}

function rank(stage: string): number {
	return (PIPELINE_STAGES as readonly string[]).indexOf(stage);
}

function emptyStage(stage: PipelineStage): StageFlow {
	return { stage, entered: 0, promoted: 0, archived: 0, demoted: 0, passRate: null };
}

export function summarizeFlows(flows: Array<Record<string, unknown>> | null | undefined): FlowSummary {
	const byStage = Object.fromEntries(PIPELINE_STAGES.map((s) => [s, emptyStage(s)])) as Record<PipelineStage, StageFlow>;
	let revived = 0;
	for (const flow of flows ?? []) {
		const count = Number(flow?.count ?? 0);
		if (!Number.isFinite(count) || count <= 0) continue;
		const from = normalizeStage(String(flow?.from_state ?? ''));
		const to = normalizeStage(String(flow?.to_state ?? ''));
		if (from === to) continue;
		const fromRank = rank(from);
		const toRank = rank(to);
		if (TERMINAL.has(from)) {
			if (toRank >= 0) revived += count;
			continue;
		}
		if (fromRank < 0) continue;
		const source = byStage[from as PipelineStage];
		if (TERMINAL.has(to)) {
			source.archived += count;
		} else if (toRank > fromRank) {
			source.promoted += count;
			byStage[to as PipelineStage].entered += count;
		} else if (toRank >= 0) {
			source.demoted += count;
		}
	}
	let archivedTotal = 0;
	for (const stage of PIPELINE_STAGES) {
		const entry = byStage[stage];
		const decided = entry.promoted + entry.archived;
		entry.passRate = decided > 0 ? entry.promoted / decided : null;
		archivedTotal += entry.archived;
	}
	const qs = byStage.quick_screen;
	return { byStage, screened: qs.promoted + qs.archived, archivedTotal, revived };
}

/** Canonical stage counts from the report's raw stage column (graveyard total included). */
export function stageTotals(stageCounts: Record<string, unknown> | null | undefined): Record<string, number> {
	const out: Record<string, number> = {};
	for (const [raw, value] of Object.entries(stageCounts ?? {})) {
		const count = Number(value);
		if (!Number.isFinite(count)) continue;
		const key = raw.trim().toLowerCase();
		// Strategy Creator scratch rows are not pipeline strategies.
		if (key === 'prebuilt' || key === 'template' || key === 'reference' || key === 'catalog') continue;
		const stage = normalizeStage(key);
		out[stage] = (out[stage] ?? 0) + count;
	}
	return out;
}

export function graveyardTotal(totals: Record<string, number>): number {
	return (totals.archived ?? 0) + (totals.rejected ?? 0) + (totals.backtest_failed ?? 0);
}
