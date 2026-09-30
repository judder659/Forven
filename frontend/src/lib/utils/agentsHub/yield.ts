// What the agents produced: strategies they created in the window, how far each
// got through the pipeline, and why the rest stopped. Causes come from the
// Forge's classifier so the graveyard and this page never disagree.

import type { AgentYield, YieldStrategy } from '$lib/api/agentsHub';
import { CAUSES, archivedFromStage, classifyArchive, tallyCauses, type Cause, type CauseCount } from '$lib/utils/forge/causes';
import { normalizeStage } from '$lib/utils/strategy';

export type Reach = 'quick_screen' | 'gauntlet' | 'paper' | 'live';

const REACH_ORDER: Reach[] = ['quick_screen', 'gauntlet', 'paper', 'live'];

export interface YieldExample {
	id: string;
	label: string;
	clause: string;
}

export interface AgentYieldSummary {
	agentId: string;
	created: number;
	ideas: number;
	spend: number | null;
	costPerStrategy: number | null;
	/** Strategies that got at least this far (a strategy on paper also reached the gauntlet). */
	reached: Record<Reach, number>;
	/** Where they are now. */
	active: number;
	graveyard: number;
	causes: CauseCount[];
	/** Archivals judged on merit vs. never fairly tested. */
	judged: number;
	untested: number;
	examples: YieldExample[];
	models: Array<{ model: string; created: number; reachedGauntlet: number }>;
}

function rank(reach: Reach): number {
	return REACH_ORDER.indexOf(reach);
}

function stageReach(stage: string): Reach | null {
	if (stage === 'live_graduated') return 'live';
	if (stage === 'paper') return 'paper';
	if (stage === 'gauntlet') return 'gauntlet';
	if (stage === 'quick_screen') return 'quick_screen';
	return null;
}

/** The furthest stage one strategy got to, from its stage now or the stage it was archived from. */
export function furthestReach(strategy: YieldStrategy): Reach {
	const stage = normalizeStage(strategy.stage);
	let best: Reach = stageReach(stage) ?? 'quick_screen';
	if (stage === 'archived' || stage === 'rejected') {
		const from = stageReach(archivedFromStage(strategy.notes) ?? '');
		if (from && rank(from) > rank(best)) best = from;
	}
	if (strategy.gauntlet_seen && rank(best) < rank('gauntlet')) best = 'gauntlet';
	return best;
}

export function summarizeYield(data: AgentYield): AgentYieldSummary[] {
	const byAgent = new Map<string, YieldStrategy[]>();
	for (const strategy of data.strategies) {
		const key = strategy.agent_id || 'unknown';
		const list = byAgent.get(key) ?? [];
		list.push(strategy);
		byAgent.set(key, list);
	}
	const summaries: AgentYieldSummary[] = [];
	for (const [agentId, strategies] of byAgent) {
		const reached: Record<Reach, number> = { quick_screen: 0, gauntlet: 0, paper: 0, live: 0 };
		const causes: Cause[] = [];
		const examples: YieldExample[] = [];
		const models = new Map<string, { created: number; reachedGauntlet: number }>();
		let active = 0;
		for (const strategy of strategies) {
			const reach = furthestReach(strategy);
			for (const step of REACH_ORDER) if (rank(step) <= rank(reach)) reached[step] += 1;
			const modelKey = strategy.model || 'Unknown model';
			const model = models.get(modelKey) ?? { created: 0, reachedGauntlet: 0 };
			model.created += 1;
			if (rank(reach) >= rank('gauntlet')) model.reachedGauntlet += 1;
			models.set(modelKey, model);
			const stage = normalizeStage(strategy.stage);
			if (stage === 'archived' || stage === 'rejected') {
				const cause = classifyArchive(strategy.notes, strategy.status_reason);
				causes.push(cause);
				if (examples.length < 3 && cause.clause) examples.push({ id: strategy.id, label: cause.label, clause: cause.clause });
			} else {
				active += 1;
			}
		}
		const tallied = tallyCauses(causes);
		const untested = causes.filter((cause) => !CAUSES[cause.key].merit).length;
		const spend = typeof data.spend[agentId] === 'number' ? data.spend[agentId] : null;
		summaries.push({
			agentId,
			created: strategies.length,
			ideas: data.ideas[agentId] ?? 0,
			spend,
			costPerStrategy: spend !== null && strategies.length > 0 ? spend / strategies.length : null,
			reached,
			active,
			graveyard: causes.length,
			causes: tallied,
			judged: causes.length - untested,
			untested,
			examples,
			models: [...models.entries()]
				.map(([model, counts]) => ({ model, ...counts }))
				.sort((a, b) => b.created - a.created),
		});
	}
	return summaries.sort((a, b) => b.created - a.created);
}
