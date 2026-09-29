/**
 * The Health view's words: the one-sentence verdict, the "needs attention"
 * grouping and each row's "why", and the /data nav indicator. All inputs are
 * server assessments (SLA state, lag, allowance); nothing is reclassified here.
 */
import type { SystemNavIndicator } from '$lib/api';
import type { ConsumerRef, ConsumerSummary, SlaCensus, SlaSeriesRow, SlaState, SlaTier } from '$lib/api/dataManagerTypes';
import { formatCount, formatDuration, plural, streamLabel, TIER_LABEL, TIERS } from './format';

export type Tone = 'ok' | 'warn' | 'bad' | 'neutral';

export interface Verdict {
	tone: Tone;
	headline: string;
	/** Secondary facts (pipeline, research), calm and short. */
	details: string[];
}

export const PROBLEM_STATES: SlaState[] = ['late', 'breach', 'missing'];
const isProblem = (state: SlaState) => PROBLEM_STATES.includes(state);

export function tierProblems(census: Pick<SlaCensus, 'by_tier'>, tier: SlaTier): { late: number; bad: number; total: number } {
	const counts = census.by_tier[tier] ?? ({} as Record<SlaState, number>);
	const late = counts.late ?? 0;
	const bad = (counts.breach ?? 0) + (counts.missing ?? 0);
	return { late, bad, total: late + bad };
}

export function tierTotal(census: Pick<SlaCensus, 'by_tier'>, tier: SlaTier): number {
	return Object.values(census.by_tier[tier] ?? {}).reduce((sum, n) => sum + (n ?? 0), 0);
}

/** "BTC-USDT 1h", "ETH-USDT funding 8h", "BTC-USDT 4h (HL perp)". */
export function seriesName(row: Pick<SlaSeriesRow, 'symbol' | 'timeframe' | 'stream' | 'venue'>): string {
	const stream = row.stream === 'ohlcv' ? '' : ` ${streamLabel(row.stream).toLowerCase()}`;
	const venue = row.venue === 'canonical' ? '' : ` (${row.venue})`;
	return `${row.symbol}${stream} ${row.timeframe}${venue}`;
}

function consumerName(ref: ConsumerRef): string {
	if (ref.kind === 'bot') return `Bot ${ref.id}`;
	if (ref.kind === 'workflow') return `Workflow ${ref.id}`;
	return `strategy ${ref.id}`;
}

function capitalize(text: string): string {
	return text.charAt(0).toUpperCase() + text.slice(1);
}

/** The one-sentence answer to "is my data OK?". `rows` may be any subset of
 * the problem series (census worst, catalog page): counts come from `by_tier`. */
export function healthVerdict(census: SlaCensus, rows: SlaSeriesRow[] = []): Verdict {
	if (!census.total) return { tone: 'neutral', headline: 'No market data is stored yet', details: [] };
	const live = tierProblems(census, 'live');
	const paper = tierProblems(census, 'paper');
	const pipeline = tierProblems(census, 'pipeline');
	const research = tierProblems(census, 'universe');
	const details: string[] = [];
	let tone: Tone = 'ok';
	let headline: string;

	const single = (tier: 'live' | 'paper') => rows.filter((row) => row.sla.tier === tier && isProblem(row.sla.state));
	if (live.total || paper.total) {
		const tier = live.total ? 'live' : 'paper';
		const counts = live.total ? live : paper;
		tone = counts.bad ? 'bad' : 'warn';
		const known = single(tier);
		const row = counts.total === 1 && known.length === 1 ? known[0] : null;
		const consumer = row?.consumers.top.find((ref) => ref.kind !== 'workflow') ?? row?.consumers.top[0];
		if (row && consumer) {
			const who = `${capitalize(TIER_LABEL[tier].toLowerCase())} ${consumerName(consumer)}`;
			headline = row.sla.state === 'missing'
				? `${who} has no data for ${seriesName(row)}`
				: `${who} is trading on data ${formatDuration(row.sla.lag_seconds)} stale`;
		} else {
			headline = `${plural(counts.total, 'series', 'series')} that ${tier} strategies trade on ${counts.total === 1 ? 'is' : 'are'} late`;
		}
		if (live.total && paper.total) details.push(`Paper: ${plural(paper.total, 'series', 'series')} late`);
	} else if (!tierTotal(census, 'live') && !tierTotal(census, 'paper')) {
		headline = 'No live or paper strategy reads market data yet';
		tone = 'neutral';
	} else {
		headline = 'Live and paper data is current';
	}
	if (pipeline.total) details.push(`Pipeline: ${plural(pipeline.total, 'series', 'series')} late`);
	if (research.total) details.push(`Research: ${formatCount(research.total)} late`);
	if (tone === 'ok' && pipeline.bad) tone = 'warn';
	return { tone, headline, details };
}

export interface AttentionGroup {
	tier: SlaTier;
	rows: SlaSeriesRow[];
	/** Problem series in the tier, from the census counts. */
	total: number;
	/** How many of them are not in `rows`. */
	hidden: number;
}

/** Problem rows grouped by tier (live first), most overdue first, capped per group. */
export function groupAttention(rows: SlaSeriesRow[], census: Pick<SlaCensus, 'by_tier'>, perGroup = 6): AttentionGroup[] {
	const seen = new Set<string>();
	const groups: AttentionGroup[] = [];
	for (const tier of TIERS) {
		const inTier = rows
			.filter((row) => row.sla.tier === tier && !row.frozen && isProblem(row.sla.state))
			.filter((row) => {
				const key = `${row.stream}:${row.venue}:${row.symbol}:${row.timeframe}`;
				if (seen.has(key)) return false;
				seen.add(key);
				return true;
			})
			.sort((a, b) => b.sla.priority - a.sla.priority);
		const total = Math.max(tierProblems(census, tier).total, inTier.length);
		if (!total) continue;
		const shown = inTier.slice(0, perGroup);
		groups.push({ tier, rows: shown, total, hidden: total - shown.length });
	}
	return groups;
}

/** "S04928 (paper)", "S01566, S06151 and B007 (live)", "" */
export function consumersText(summary: ConsumerSummary, max = 2): string {
	if (!summary.count) return '';
	const names = summary.top.slice(0, max).map((ref) => ref.id);
	const rest = summary.count - names.length;
	const list = rest > 0 ? `${names.join(', ')} and ${rest} more` : names.length > 1 ? `${names.slice(0, -1).join(', ')} and ${names.at(-1)}` : names[0];
	return `${list} (${TIER_LABEL[summary.tier].toLowerCase()})`;
}

/** Why a row needs attention: "allowed 45 min, 52 min behind; feeds S04928 (paper)". */
export function whyText(row: Pick<SlaSeriesRow, 'sla' | 'consumers'>): string {
	const who = consumersText(row.consumers);
	const feeds = who ? `feeds ${who}` : row.sla.tier === 'universe' ? 'in the research universe' : 'nothing reads it';
	if (row.sla.state === 'missing') return `nothing stored yet; ${who ? `needed by ${who}` : feeds}`;
	return `allowed ${formatDuration(row.sla.allowed_seconds)}, ${formatDuration(row.sla.lag_seconds)} behind; ${feeds}`;
}

// ---------------------------------------------------------------- the /data nav indicator

function emptyIndicator(): SystemNavIndicator {
	return { kind: 'none', severity: 'neutral', label: '', summary: '', count: 0, seen_key: '' };
}

/** Live and paper problems only: the sidebar never shows research counts.
 * A live series past the breach limit (or missing) is a standing hazard and
 * shows as a status pill; anything else is a count that clears once seen. */
export function buildDataNavIndicator(census: SlaCensus): SystemNavIndicator {
	const live = tierProblems(census, 'live');
	const paper = tierProblems(census, 'paper');
	const total = live.total + paper.total;
	if (!total) return emptyIndicator();
	const ids = census.worst
		.filter((row) => (row.sla.tier === 'live' || row.sla.tier === 'paper') && isProblem(row.sla.state))
		.map((row) => `${row.stream}:${row.venue}:${row.symbol}:${row.timeframe}`)
		.sort()
		.slice(0, 8);
	const seenKey = `data-sla:${[live.bad, live.late, paper.bad, paper.late].join('.')}${ids.length ? `:${ids.join('|')}` : ''}`;
	const parts = [live.total ? `${plural(live.total, 'live series', 'live series')}` : '', paper.total ? `${plural(paper.total, 'paper series', 'paper series')}` : '']
		.filter(Boolean)
		.join(' and ');
	const summary = `${parts} ${total === 1 ? 'is' : 'are'} late`;
	if (live.bad) {
		return { kind: 'status', severity: 'danger', label: 'STALE', summary, count: total, seen_key: seenKey };
	}
	return {
		kind: 'count',
		severity: live.late || paper.bad ? 'danger' : 'warn',
		label: String(total),
		summary,
		count: total,
		seen_key: seenKey,
	};
}
