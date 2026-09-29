// Why a strategy ended up in the graveyard, read from the text the pipeline
// wrote when it archived it (strategies.notes, and the reason on the matching
// strategy_events row; both carry the same sentence). The sentence is built as
//   "Retired from <stage> to archived by <actor>. Reason: … Trigger: <gate verdict>.
//    Robustness reading: … Metric snapshot: … Snapshot at retirement: …"
// The trailing snapshots mention every metric (IS Sharpe, Trades, Robustness…),
// so the cause is read from the verdict clause alone — never from the snapshots.

import { normalizeStage } from '$lib/utils/strategy';

export type CauseKey =
	| 'below_bar'
	| 'overfit'
	| 'negative_edge'
	| 'few_trades'
	| 'validation'
	| 'holdout'
	| 'untestable'
	| 'not_judged'
	| 'code_error'
	| 'operator'
	| 'retired'
	| 'agent'
	| 'other';

export interface CauseMeta {
	key: CauseKey;
	label: string;
	/** Whether it was judged on merit (vs. never fairly tested). */
	merit: boolean;
	help: string;
}

export const CAUSES: Record<CauseKey, CauseMeta> = {
	below_bar: { key: 'below_bar', label: 'Missed quick-screen bars', merit: true, help: 'Return, Sharpe, profit factor or drawdown missed the screen thresholds.' },
	overfit: { key: 'overfit', label: 'In/out-of-sample mismatch', merit: true, help: 'In-sample and out-of-sample results disagreed — the overfit (selection-bias) screen.' },
	negative_edge: { key: 'negative_edge', label: 'Losing edge', merit: true, help: 'Negative Sharpe or return, or a negative robustness reading.' },
	few_trades: { key: 'few_trades', label: 'Too few trades', merit: true, help: 'Not enough trades for the result to mean anything.' },
	validation: { key: 'validation', label: 'Failed validation', merit: true, help: 'A gauntlet test (walk-forward, jitter, cost stress, Monte Carlo, regime split) failed.' },
	holdout: { key: 'holdout', label: 'Held-back test', merit: true, help: 'The one-shot held-back test decided against it (or had not finished).' },
	untestable: { key: 'untestable', label: 'Untestable', merit: false, help: 'Archived without a fair test: missing data, not enough history, no signals or broken code.' },
	not_judged: { key: 'not_judged', label: 'Never judged', merit: false, help: 'Retries ran out on a transient block; the gate never judged it.' },
	code_error: { key: 'code_error', label: 'Code or data error', merit: false, help: 'Signal code crashed, or needed data or history the market did not have.' },
	operator: { key: 'operator', label: 'Archived by you', merit: false, help: 'Moved to the graveyard from the Forge or a strategy page.' },
	retired: { key: 'retired', label: 'Retired from paper/live', merit: true, help: 'Dethroned, decayed or demoted after running forward.' },
	agent: { key: 'agent', label: 'Rejected by an agent', merit: true, help: 'An agent rejected it after reviewing the evidence.' },
	other: { key: 'other', label: 'Other', merit: false, help: 'No recognisable archival reason was recorded.' },
};

/** Display order for the cause breakdown. */
export const CAUSE_ORDER: CauseKey[] = [
	'below_bar',
	'overfit',
	'negative_edge',
	'few_trades',
	'validation',
	'holdout',
	'retired',
	'agent',
	'untestable',
	'code_error',
	'not_judged',
	'operator',
	'other',
];

const UNTESTABLE_LABELS: Record<string, string> = {
	insufficient_history: 'not enough history',
	no_signal: 'too few signals',
	broken_code: 'broken code',
	lookahead: 'lookahead leak',
	parked: 'parked lane retired',
	uncertified: 'uncertified',
	missing_data: 'missing data',
};

const METRIC_LABELS: Array<[RegExp, string]> = [
	[/profit_factor/, 'profit factor'],
	[/\bsharpe\b/i, 'Sharpe'],
	[/total_return_pct/, 'return'],
	[/max_drawdown_pct/, 'drawdown'],
	[/win_rate/, 'win rate'],
];

const TAIL = /(?:\.\s+|;\s+|\s+)(?:Robustness reading|Metric snapshot|Snapshot at retirement|Stability warning)\b/;

/** The verdict clause of an archival sentence, without the boilerplate around it. */
export function primaryClause(text: string | null | undefined): string {
	let out = String(text ?? '').trim();
	if (!out) return '';
	const trigger = out.lastIndexOf('Trigger:');
	if (trigger >= 0) out = out.slice(trigger + 'Trigger:'.length);
	else {
		const reason = out.indexOf('Reason:');
		if (reason >= 0) out = out.slice(reason + 'Reason:'.length);
	}
	const tail = out.search(TAIL);
	if (tail >= 0) out = out.slice(0, tail);
	out = out
		.trim()
		.replace(/^Gauntlet failed_gate:\s*/i, '')
		.replace(/^Pipeline hygiene:\s*/i, '')
		.replace(/^Terminal gate failure:\s*/i, '')
		.replace(/^quick_screen→gauntlet blocked:\s*/i, '')
		.replace(/^Failure transition .*? by \S+\.\s*/i, '')
		.replace(/\s+/g, ' ')
		.replace(/[.\s]+$/, '');
	return out;
}

export interface Cause {
	key: CauseKey;
	label: string;
	/** Short qualifier, e.g. "profit factor · Sharpe" or "not enough history". */
	detail: string;
	/** The verdict clause the classification was read from. */
	clause: string;
}

function build(key: CauseKey, detail: string, clause: string): Cause {
	return { key, label: CAUSES[key].label, detail, clause };
}

/**
 * Classify one archival. `notes` is the archival sentence; `statusReason` is the
 * `untestable:<code>: <why>` marker the pipeline stamps on untested archives.
 */
export function classifyArchive(notes: string | null | undefined, statusReason?: string | null): Cause {
	const status = String(statusReason ?? '').trim();
	const text = String(notes ?? '').trim();
	const clause = primaryClause(text);
	const whole = `${text} ${status}`;

	const untestable =
		/^untestable:([a-z_]+)/i.exec(status) ||
		/Untestable \(([a-z_]+)\)/i.exec(clause) ||
		/archived as untestable \(([a-z_]+)\)/i.exec(text);
	if (untestable) {
		const code = untestable[1].toLowerCase();
		return build('untestable', UNTESTABLE_LABELS[code] ?? code.replace(/_/g, ' '), clause || status);
	}
	if (/Retries exhausted|NOT a merit verdict|never judged/i.test(clause)) return build('not_judged', 'retries exhausted', clause);
	if (/no registered runtime class|orphan/i.test(clause)) return build('not_judged', 'no runtime class', clause);
	if (/Indicator execution failed|Traceback|KeyError|TypeError|ValueError|Exception/i.test(clause)) {
		const missing = /KeyError: '([^']+)'/.exec(clause);
		return build('code_error', missing ? `missing ${missing[1].replace(/_/g, ' ')}` : 'signal code crashed', clause);
	}
	if (/lookback \(\d+\) exceeds|exceeds or equals available bars/i.test(clause)) return build('code_error', 'lookback longer than the data', clause);

	const test = /\b(walk_forward|parameter_jitter|cost_stress|monte_carlo|regime_split) verdict failed/i.exec(clause);
	if (test) return build('validation', test[1].replace(/_/g, ' '), clause);
	if (/Walk-forward pass rate|S00552 REJECT/i.test(clause)) return build('validation', 'walk forward', clause);
	if (/Held-back|holdout/i.test(clause)) return build('holdout', '', clause);

	if (/Batch archived|User moved|moved to graveyard|manually|to archived by (?:ui|api|operator|manual)\b/i.test(whole)) return build('operator', '', clause);
	if (/dethrone|decay|kill.?switch|demot/i.test(clause)) return build('retired', '', clause);
	if (/Brain promotion to rejected|rejected by brain/i.test(whole)) return build('agent', 'Brain', clause);

	if (/selection bias|but OOS/i.test(clause)) return build('overfit', '', clause);
	if (/Negative sharpe|IS Sharpe -?[\d.]+ < 0\b|Robustness -?[\d.]+ < 0\b/i.test(clause)) return build('negative_edge', '', clause);
	if (/Trades \d+ < \d+|zero trades|no signals|produces no signals/i.test(clause)) return build('few_trades', '', clause);

	const metrics = METRIC_LABELS.filter(([pattern]) => pattern.test(clause)).map(([, label]) => label);
	if (metrics.length > 0 && /[<>]/.test(clause)) return build('below_bar', metrics.join(' · '), clause);

	return build('other', '', clause);
}

/** The stage a strategy was archived FROM, when the archival sentence says so. */
export function archivedFromStage(notes: string | null | undefined): string | null {
	const text = String(notes ?? '');
	const match =
		/Retired from ([a-z_]+) to /i.exec(text) ||
		/transition ([a-z_]+) -> /i.exec(text) ||
		/moving \S+ from ([a-z_]+) to /i.exec(text);
	if (!match) return null;
	const raw = match[1].toLowerCase();
	return raw.startsWith('research') ? 'research_only' : normalizeStage(raw);
}

export interface CauseCount {
	key: CauseKey;
	label: string;
	count: number;
	share: number;
	/** The most common qualifier inside the bucket (e.g. "profit factor"). */
	topDetail: string;
}

/** Bucket counts, largest first, for a set of already-classified archives. */
export function tallyCauses(causes: Cause[]): CauseCount[] {
	const counts = new Map<CauseKey, { count: number; details: Map<string, number> }>();
	for (const cause of causes) {
		const entry = counts.get(cause.key) ?? { count: 0, details: new Map<string, number>() };
		entry.count += 1;
		for (const part of cause.detail.split(' · ').filter(Boolean)) {
			entry.details.set(part, (entry.details.get(part) ?? 0) + 1);
		}
		counts.set(cause.key, entry);
	}
	const total = causes.length || 1;
	return [...counts.entries()]
		.map(([key, entry]) => {
			const topDetail = [...entry.details.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? '';
			return { key, label: CAUSES[key].label, count: entry.count, share: entry.count / total, topDetail };
		})
		.sort((a, b) => b.count - a.count || CAUSE_ORDER.indexOf(a.key) - CAUSE_ORDER.indexOf(b.key));
}
