// The Agents page's answer to "is my AI workforce working, and is it worth it?",
// generated from the same numbers the panels show. One string per sentence so
// the page can style the lead and drop sentences whose data is missing.

import type { AgentFleet } from '$lib/api/agentsHub';
import type { AgentYieldSummary } from './yield';
import { fmtCost, fmtRate, plural } from './format';

export interface HeadlineInput {
	fleet: AgentFleet;
	/** The top producer's yield over the same window, or null while it loads. */
	yieldTop: AgentYieldSummary | null;
	producerName: string | null;
	/** Items in "Needs you", or null while provider health is still loading. */
	attention: number | null;
}

function windowWords(window: string): string {
	return window === '7d' ? 'the last 7 days' : 'the last 24 hours';
}

export function fleetHeadline(input: HeadlineInput): string[] {
	const { fleet } = input;
	const totals = fleet.totals;
	const sentences: string[] = [];

	const parts: string[] = [];
	if (totals.running > 0) parts.push(`${totals.running} working`);
	if (totals.pending > 0) parts.push(`${totals.pending} queued`);
	if (totals.paused > 0) parts.push(`${totals.paused} paused`);
	if (parts.length === 0) sentences.push(`All ${plural(totals.agents, 'agent')} are idle.`);
	else sentences.push(`${plural(totals.agents, 'agent')}: ${parts.join(', ')}.`);

	if (totals.runs === 0) {
		sentences.push(`No runs finished in ${windowWords(fleet.window)}.`);
	} else {
		// Same definition as each agent's rate: cancelled runs are not judged.
		const judged = fleet.agents.reduce((sum, agent) => sum + agent.window.ok + agent.window.failed + agent.window.blocked, 0);
		const rate = judged > 0 ? ` (${fmtRate(totals.ok / judged)} succeeded)` : '';
		const spend = fleet.window === '7d' ? `${fmtCost(totals.spend_d7)} over 7 days` : `${fmtCost(totals.spend_today)} today`;
		sentences.push(`In ${windowWords(fleet.window)} they finished ${plural(totals.runs, 'run')}${rate} and spent ${spend}.`);
	}

	const top = input.yieldTop;
	if (top && top.created > 0) {
		const who = input.producerName ?? 'Agents';
		const from = top.ideas > 0 ? ` from ${plural(top.ideas, 'idea')}` : '';
		const g = top.reached.gauntlet;
		const p = top.reached.paper;
		let fate: string;
		if (g === 0) fate = 'none has passed the quick screen yet';
		else if (p === 0) fate = `${g} reached the gauntlet, none reached paper`;
		else fate = `${g} reached the gauntlet and ${p} reached paper`;
		sentences.push(`${who} built ${plural(top.created, 'strategy', 'strategies')}${from}: ${fate}.`);
	}

	if (input.attention !== null) {
		sentences.push(input.attention === 0 ? 'Nothing needs you.' : `${plural(input.attention, 'thing')} ${input.attention === 1 ? 'needs' : 'need'} you.`);
	}
	return sentences;
}
