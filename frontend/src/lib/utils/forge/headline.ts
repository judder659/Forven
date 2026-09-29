// The Forge's one-paragraph answer to "what is going on?", generated from the
// same numbers the panels below show. Each sentence is a separate string so the
// page can style the lead sentence and drop sentences whose data is missing.

import type { FlowSummary } from './flow';

export interface HeadlineInput {
	live: number;
	paper: number;
	gauntlet: number;
	quickScreen: number;
	/** Flow over the selected window, or null while it loads. */
	flow: FlowSummary | null;
	windowLabel: string;
	/** Items in "Needs you", or null while the explainer is still running. */
	attention: number | null;
}

function plural(n: number, one: string, many = `${one}s`): string {
	return `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;
}

export function forgeHeadline(input: HeadlineInput): string[] {
	const sentences: string[] = [];
	const trading = input.live + input.paper;
	if (trading === 0) sentences.push('Nothing is trading forward yet.');
	else if (input.live === 0) sentences.push(`${plural(input.paper, 'strategy', 'strategies')} on paper, none live yet.`);
	else if (input.paper === 0) sentences.push(`${input.live} live, none on paper.`);
	else sentences.push(`${input.live} live and ${input.paper} on paper.`);

	const flow = input.flow;
	if (flow) {
		const qs = flow.byStage.quick_screen;
		const g = flow.byStage.gauntlet;
		const p = flow.byStage.paper;
		if (flow.screened === 0 && g.promoted + g.archived === 0) {
			sentences.push(`No ideas were screened in the last ${input.windowLabel}.`);
		} else {
			const parts: string[] = [];
			parts.push(`${qs.promoted} passed the quick screen`);
			if (g.promoted > 0) parts.push(`${g.promoted} cleared the gauntlet`);
			else parts.push(qs.promoted > 0 || g.archived > 0 ? 'none cleared the gauntlet' : '');
			if (p.promoted > 0) parts.push(`${p.promoted} went live`);
			const joined = parts.filter(Boolean);
			const tail = joined.length > 1 ? `${joined.slice(0, -1).join(', ')} and ${joined[joined.length - 1]}` : joined[0];
			sentences.push(`In the last ${input.windowLabel} the factory screened ${plural(flow.screened, 'idea')}: ${tail}.`);
		}
	}

	if (input.attention !== null) {
		sentences.push(input.attention === 0 ? 'Nothing needs you.' : `${plural(input.attention, 'thing')} ${input.attention === 1 ? 'needs' : 'need'} you.`);
	}
	return sentences;
}
