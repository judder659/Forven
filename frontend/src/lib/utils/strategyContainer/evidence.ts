// Robustness evidence for the strategy container: the gauntlet rollup, the held-back
// test, the gate explanation, and each persisted robustness payload, read into one
// model the Summary scorecard and the Robustness stress matrix both render.
//
// Payload units (as the engine persists them): Monte Carlo probabilities and
// distributions are percent points; jitter pass rate / thresholds, cost-stress
// threshold, regime share and WFA max degradation are fractions; cost-stress
// degradation is percent points; regime win rate and average return are percent
// points.

import { getRobustnessResult, getHoldoutSummary, type HoldoutSummary, type PersistedRobustnessResult } from '$lib/api/backtesting';
import { explainStrategy, getGauntletStatus, type GauntletStatus, type GauntletTestEntry, type GauntletTestKey } from '$lib/api/lifecycle';
import { fmtDateUtc, isNum, toNumber } from './format';

type Bag = Record<string, unknown>;

function asBag(value: unknown): Bag {
	return value && typeof value === 'object' && !Array.isArray(value) ? (value as Bag) : {};
}

export const TEST_KEYS: GauntletTestKey[] = ['walk_forward', 'monte_carlo', 'parameter_jitter', 'cost_stress', 'regime_split'];

type ExplainResponse = Awaited<ReturnType<typeof explainStrategy>>;

export interface ContainerEvidence {
	gauntlet: GauntletStatus | null;
	holdout: HoldoutSummary | null;
	explain: ExplainResponse['strategy'] | null;
	payloads: Partial<Record<GauntletTestKey, PersistedRobustnessResult>>;
}

export const EMPTY_EVIDENCE: ContainerEvidence = { gauntlet: null, holdout: null, explain: null, payloads: {} };

/**
 * Every request runs in parallel and any failure reads as "not available": a
 * missing piece of evidence must never blank the page.
 */
export async function loadContainerEvidence(strategyId: string): Promise<ContainerEvidence> {
	const [gauntlet, holdout, explain] = await Promise.all([
		getGauntletStatus(strategyId).catch(() => null),
		getHoldoutSummary(strategyId).catch(() => null),
		explainStrategy(strategyId).then((response) => response?.strategy ?? null).catch(() => null),
	]);
	const payloads: Partial<Record<GauntletTestKey, PersistedRobustnessResult>> = {};
	const tests = gauntlet?.tests ?? ({} as Record<GauntletTestKey, GauntletTestEntry | null>);
	await Promise.all(
		TEST_KEYS.map(async (key) => {
			const resultId = String(tests[key]?.result_id ?? '').trim();
			if (!resultId) return;
			const persisted = await getRobustnessResult(resultId).catch(() => null);
			if (persisted && typeof persisted === 'object') payloads[key] = persisted;
		}),
	);
	return { gauntlet: gauntlet ?? null, holdout: holdout ?? null, explain, payloads };
}

// ---------------------------------------------------------------------------
// Payload readers

export interface Fold {
	fold: number;
	testStart: string | null;
	testEnd: string | null;
	isSharpe: number | null;
	oosSharpe: number | null;
	isTrades: number | null;
	oosTrades: number | null;
	/** Percent points. */
	oosReturnPct: number | null;
}

export interface Baseline {
	alphaPct: number | null;
	alphaT: number | null;
	sharpe: { strategy: number | null; buyHold: number | null; trend: number | null };
	/** Percent points. */
	returns: { strategy: number | null; buyHold: number | null; trend: number | null };
	status: string | null;
	days: number | null;
}

export function readBaseline(value: unknown): Baseline | null {
	const bag = asBag(value);
	if (Object.keys(bag).length === 0) return null;
	const sharpe = asBag(bag.sharpe);
	const returns = asBag(bag.total_return_pct);
	return {
		alphaPct: toNumber(bag.alpha_pct),
		alphaT: toNumber(bag.alpha_t),
		sharpe: { strategy: toNumber(sharpe.strategy), buyHold: toNumber(sharpe.buy_hold), trend: toNumber(sharpe.trend) },
		returns: { strategy: toNumber(returns.strategy), buyHold: toNumber(returns.buy_hold), trend: toNumber(returns.trend) },
		status: typeof bag.status === 'string' ? bag.status : null,
		days: toNumber(bag.n_days),
	};
}

export interface WalkForwardEvidence {
	folds: Fold[];
	avgIsSharpe: number | null;
	avgOosSharpe: number | null;
	/** Fraction; the gate allows OOS Sharpe to fall this much below IS. */
	maxDegradation: number | null;
	verdict: string | null;
	aggregate: Bag;
	baseline: Baseline | null;
}

export function readWalkForward(payload: unknown): WalkForwardEvidence | null {
	const bag = asBag(payload);
	const splits = Array.isArray(bag.splits) ? (bag.splits as unknown[]) : [];
	if (splits.length === 0 && bag.avg_oos_sharpe === undefined) return null;
	const folds = splits.map((split, index) => {
		const s = asBag(split);
		const range = asBag(s.date_range);
		const inSample = asBag(s.in_sample);
		const outOfSample = asBag(s.out_of_sample);
		const oosReturn = toNumber(outOfSample.total_return_pct);
		return {
			fold: toNumber(s.split) ?? index + 1,
			testStart: typeof range.split_at === 'string' ? range.split_at : null,
			testEnd: typeof range.end === 'string' ? range.end : null,
			isSharpe: toNumber(inSample.sharpe),
			oosSharpe: toNumber(outOfSample.sharpe),
			isTrades: toNumber(inSample.total_trades),
			oosTrades: toNumber(outOfSample.total_trades),
			oosReturnPct: oosReturn === null ? null : oosReturn * 100,
		};
	});
	return {
		folds,
		avgIsSharpe: toNumber(bag.avg_is_sharpe),
		avgOosSharpe: toNumber(bag.avg_oos_sharpe),
		maxDegradation: toNumber(asBag(bag.verdict_thresholds).max_degradation),
		verdict: typeof bag.verdict === 'string' ? bag.verdict : null,
		aggregate: asBag(bag.aggregate_oos),
		baseline: readBaseline(bag.baseline_hurdle),
	};
}

/** p5 / p25 / p50 / p75 / p95 of equity at each trade step across resampled paths. */
export function monteCarloFan(paths: unknown): Array<[number, number, number, number, number, number]> {
	const rows = (Array.isArray(paths) ? paths : []).filter((path): path is number[] => Array.isArray(path) && path.length > 1);
	if (rows.length < 2) return [];
	const steps = Math.min(...rows.map((path) => path.length));
	const quantile = (sorted: number[], q: number) => {
		const position = (sorted.length - 1) * q;
		const low = Math.floor(position);
		const high = Math.ceil(position);
		return sorted[low] + (sorted[high] - sorted[low]) * (position - low);
	};
	const out: Array<[number, number, number, number, number, number]> = [];
	for (let step = 0; step < steps; step += 1) {
		const column = rows.map((path) => Number(path[step])).filter(Number.isFinite).sort((a, b) => a - b);
		if (column.length === 0) continue;
		out.push([step, quantile(column, 0.05), quantile(column, 0.25), quantile(column, 0.5), quantile(column, 0.75), quantile(column, 0.95)]);
	}
	return out;
}

export interface HeldBack {
	verdict: 'PASS' | 'FAIL' | null;
	state: HoldoutSummary['state'] | null;
	start: string | null;
	end: string | null;
	bars: number | null;
	family: string | null;
	shot: number | null;
	maxShots: number | null;
	metrics: Bag;
	baseline: Baseline | null;
}

export function readHeldBack(summary: HoldoutSummary | null | undefined): HeldBack | null {
	if (!summary) return null;
	const result = asBag(summary.latest?.result);
	const heldBack = asBag(result.held_back);
	const verdict = result.verdict === 'PASS' || result.verdict === 'FAIL' ? result.verdict : summary.state === 'pass' ? 'PASS' : summary.state === 'fail' ? 'FAIL' : null;
	return {
		verdict,
		state: summary.state ?? null,
		start: typeof heldBack.start === 'string' ? heldBack.start : null,
		end: typeof heldBack.end === 'string' ? heldBack.end : null,
		bars: toNumber(heldBack.bars),
		family: typeof result.family === 'string' ? result.family : summary.family ?? null,
		shot: toNumber(result.family_shot),
		maxShots: toNumber(summary.max_family_shots),
		metrics: asBag(result.out_of_sample),
		baseline: readBaseline(result.baseline_hurdle),
	};
}

// ---------------------------------------------------------------------------
// Stress rows

export type EvidenceTone = 'ok' | 'caution' | 'fail' | 'idle';

export interface Bullet {
	min: number;
	max: number;
	value: number;
	threshold: number;
	direction: 'ge' | 'le';
}

export interface StressRow {
	key: string;
	label: string;
	question: string;
	value: string;
	bullet: Bullet | null;
	thresholdText: string;
	/** The gate's verdict as stored (PASS / FAIL / PASS* / LOW / NOT RUN). */
	verdict: string;
	tone: EvidenceTone;
	evidence: string;
	/** The evidence without the stale note, for sentences that state staleness once. */
	basis: string;
	/** Passed on thin evidence, or advisory and below the usual bar. */
	weak: boolean;
	stale: boolean;
	resultId: string | null;
	testKey: GauntletTestKey | null;
	/** Structured numbers behind the row, for sentences built elsewhere. */
	detail?: Record<string, number | null>;
}

/** Minimum reruns before a jitter verdict carries weight (the backend errors below it). */
export const MIN_JITTER_RERUNS = 10;
/** Fold trade counts below this make a fold's Sharpe a coin toss. */
export const THIN_FOLD_TRADES = 20;
/** Conventional significance bars. */
export const DSR_BAR = 0.95;
export const ALPHA_T_BAR = 2;

const pctText = (value: number, digits = 0) => `${value.toFixed(digits)}%`;
const numText = (value: number, digits = 2) => (value < 0 ? `−${Math.abs(value).toFixed(digits)}` : value.toFixed(digits));

function toneFor(verdict: string, weak: boolean, stale: boolean): EvidenceTone {
	const v = verdict.toUpperCase();
	if (v === 'NOT RUN') return 'idle';
	if (v === 'FAIL') return 'fail';
	if (v === 'LOW') return 'caution';
	return weak || stale ? 'caution' : 'ok';
}

function entryVerdict(entry: GauntletTestEntry | null | undefined, payloadVerdict: unknown): string {
	if (entry?.rescued_by_fold_pass_rate) return 'PASS*';
	const verdict = String(entry?.verdict ?? payloadVerdict ?? '').trim().toUpperCase();
	if (verdict) return verdict;
	const status = String(entry?.status ?? '').toLowerCase();
	if (status === 'passed' || status === 'succeeded') return 'PASS';
	if (status === 'failed') return 'FAIL';
	return 'NOT RUN';
}

const STALE_NOTE = 'Stale: parameters changed after this ran. ';

function withStale(evidence: string, stale: boolean): string {
	return stale ? `${STALE_NOTE}${evidence}` : evidence;
}

export function buildStressRows(evidence: ContainerEvidence): StressRow[] {
	const tests = evidence.gauntlet?.tests ?? ({} as Record<GauntletTestKey, GauntletTestEntry | null>);
	const payload = (key: GauntletTestKey) => asBag(evidence.payloads[key]?.payload);
	const rows: Array<Omit<StressRow, 'basis'>> = [];

	// Walk-forward: OOS Sharpe as a share of IS Sharpe (walk-forward efficiency).
	{
		const entry = tests.walk_forward;
		const wf = readWalkForward(payload('walk_forward'));
		const stale = entry?.stale === true;
		const verdict = entryVerdict(entry, wf?.verdict);
		const efficiency = wf && isNum(wf.avgIsSharpe) && wf.avgIsSharpe > 0 && isNum(wf.avgOosSharpe) ? (wf.avgOosSharpe / wf.avgIsSharpe) * 100 : null;
		const threshold = isNum(wf?.maxDegradation) ? (1 - (wf?.maxDegradation ?? 0)) * 100 : null;
		const trades = (wf?.folds ?? []).map((fold) => fold.oosTrades).filter(isNum);
		const worst = (wf?.folds ?? []).filter((fold) => isNum(fold.oosSharpe)).sort((a, b) => (a.oosSharpe ?? 0) - (b.oosSharpe ?? 0))[0];
		const thin = trades.length > 0 && Math.max(...trades) < THIN_FOLD_TRADES;
		const parts = [
			wf?.folds.length ? `${wf.folds.length} folds` : null,
			trades.length ? `${Math.min(...trades)}–${Math.max(...trades)} trades each` : null,
			worst && isNum(worst.oosSharpe) ? `fold ${worst.fold} Sharpe ${numText(worst.oosSharpe)}` : null,
			entry?.rescued_by_fold_pass_rate ? `fold-rescued (raw verdict ${entry.verdict_raw ?? 'FAIL'})` : null,
		].filter(Boolean);
		rows.push({
			key: 'walk_forward',
			label: 'Walk-forward',
			question: 'Does a re-fit keep working on the next unseen window?',
			value: efficiency === null ? '—' : `${pctText(efficiency)} OOS/IS Sharpe`,
			bullet: efficiency === null || threshold === null ? null : { min: 0, max: 200, value: efficiency, threshold, direction: 'ge' },
			thresholdText: threshold === null ? '' : `≥ ${pctText(threshold)}`,
			verdict,
			tone: toneFor(verdict, thin || Boolean(entry?.rescued_by_fold_pass_rate), stale),
			evidence: withStale(parts.join(' · ') || 'No walk-forward result', stale),
			weak: thin || Boolean(entry?.rescued_by_fold_pass_rate),
			stale,
			resultId: entry?.result_id ?? null,
			testKey: 'walk_forward',
		});
	}

	// Monte Carlo: probability the reshuffled trade sequence ends profitable.
	{
		const entry = tests.monte_carlo;
		const mc = payload('monte_carlo');
		const thresholds = asBag(mc.verdict_thresholds);
		const prob = toNumber(mc.prob_profitable);
		const minProb = toNumber(thresholds.min_prob_profitable);
		const ddP95 = toNumber(asBag(mc.drawdown_distribution).p95);
		const maxDd = toNumber(thresholds.max_dd_p95);
		const stale = entry?.stale === true;
		const verdict = entryVerdict(entry, mc.verdict);
		rows.push({
			key: 'monte_carlo',
			label: 'Monte Carlo',
			question: 'Is profit robust to the order and mix of trades?',
			value: prob === null ? '—' : `${pctText(prob, 1)} profitable`,
			bullet: prob === null || minProb === null ? null : { min: 0, max: 100, value: prob, threshold: minProb, direction: 'ge' },
			thresholdText: [minProb === null ? null : `≥ ${pctText(minProb)}`, ddP95 === null ? null : `P95 drawdown ${pctText(ddP95, 1)}${maxDd === null ? '' : ` ≤ ${pctText(maxDd)}`}`].filter(Boolean).join(' · '),
			verdict,
			tone: toneFor(verdict, false, stale),
			evidence: withStale(`${toNumber(mc.n_simulations)?.toLocaleString('en-US') ?? '—'} resamples of ${toNumber(mc.n_trades) ?? '—'} trades`, stale),
			weak: false,
			stale,
			resultId: entry?.result_id ?? null,
			testKey: 'monte_carlo',
		});
	}

	// Parameter jitter: share of nudged reruns that kept enough of the Sharpe.
	{
		const entry = tests.parameter_jitter;
		const jit = payload('parameter_jitter');
		const passRate = toNumber(jit.pass_rate);
		const threshold = toNumber(jit.verdict_threshold);
		const planned = toNumber(jit.n_iterations);
		const completed = toNumber(jit.iterations_completed) ?? (Array.isArray(jit.sharpe_values) ? jit.sharpe_values.length : null);
		const weak = completed !== null && completed < MIN_JITTER_RERUNS;
		const stale = entry?.stale === true;
		const verdict = entryVerdict(entry, jit.verdict);
		rows.push({
			key: 'parameter_jitter',
			label: 'Parameter jitter',
			question: 'Does it survive small parameter changes?',
			value: passRate === null ? '—' : `${pctText(passRate * 100)} of reruns held`,
			bullet: passRate === null || threshold === null ? null : { min: 0, max: 100, value: passRate * 100, threshold: threshold * 100, direction: 'ge' },
			thresholdText: threshold === null ? '' : `≥ ${pctText(threshold * 100)} keep half the Sharpe`,
			verdict,
			tone: toneFor(verdict, weak, stale),
			evidence: withStale(completed === null ? 'Rerun count not recorded' : `${completed} of ${planned ?? '?'} reruns finished${jit.deadline_hit === true ? ' (time limit)' : ''}`, stale),
			weak,
			stale,
			resultId: entry?.result_id ?? null,
			testKey: 'parameter_jitter',
		});
	}

	// Cost stress: Sharpe lost at multiplied fees and slippage.
	{
		const entry = tests.cost_stress;
		const cost = payload('cost_stress');
		const degradation = toNumber(cost.degradation_pct);
		const threshold = toNumber(cost.verdict_threshold);
		const stale = entry?.stale === true;
		const verdict = entryVerdict(entry, cost.verdict);
		const trades = toNumber(asBag(cost.original).total_trades);
		rows.push({
			key: 'cost_stress',
			label: 'Cost stress',
			question: `Does it survive ${toNumber(cost.fee_multiplier) ?? 2}× fees and slippage?`,
			value: degradation === null ? '—' : `−${pctText(degradation, 1)} Sharpe`,
			bullet: degradation === null || threshold === null ? null : { min: 0, max: Math.max(60, threshold * 200), value: degradation, threshold: threshold * 100, direction: 'le' },
			thresholdText: threshold === null ? '' : `≤ ${pctText(threshold * 100)} lost`,
			verdict,
			tone: toneFor(verdict, false, stale),
			evidence: withStale(trades === null ? 'Trade count not recorded' : `${trades} trades, the baseline run's window`, stale),
			weak: false,
			stale,
			resultId: entry?.result_id ?? null,
			testKey: 'cost_stress',
			detail: {
				multiplier: toNumber(cost.fee_multiplier),
				degradationPct: degradation,
				thresholdPct: threshold === null ? null : threshold * 100,
			},
		});
	}

	// Regime split: share of scored regimes that made money.
	{
		const entry = tests.regime_split;
		const regime = payload('regime_split');
		const share = toNumber(regime.profitable_regime_share);
		const threshold = toNumber(regime.verdict_threshold);
		const regimes = Array.isArray(regime.regimes) ? (regime.regimes as Bag[]) : [];
		const dropped = Array.isArray(regime.dropped_low_trade_regimes) ? (regime.dropped_low_trade_regimes as string[]) : [];
		const stale = entry?.stale === true;
		const verdict = entryVerdict(entry, regime.verdict);
		rows.push({
			key: 'regime_split',
			label: 'Regime split',
			question: 'Does it make money in more than one market regime?',
			value: share === null ? '—' : `${pctText(share * 100)} of regimes profitable`,
			bullet: share === null || threshold === null ? null : { min: 0, max: 100, value: share * 100, threshold: threshold * 100, direction: 'ge' },
			thresholdText: threshold === null ? '' : `≥ ${pctText(threshold * 100)}`,
			verdict,
			tone: toneFor(verdict, false, stale),
			evidence: withStale(
				[`${regimes.length - dropped.length} regimes scored`, dropped.length ? `${dropped.map((name) => name.replace(/_/g, ' ').toLowerCase()).join(', ')} dropped (< ${toNumber(regime.regime_min_trades) ?? 5} trades)` : null].filter(Boolean).join(' · '),
				stale,
			),
			weak: false,
			stale,
			resultId: entry?.result_id ?? null,
			testKey: 'regime_split',
		});
	}

	// Held-back test: the one-shot run on data sealed from research.
	{
		const held = readHeldBack(evidence.holdout);
		const verdict = held?.verdict ?? (held?.state === 'exempt' ? 'EXEMPT' : held?.state === 'running' ? 'RUNNING' : 'NOT RUN');
		const ret = toNumber(held?.metrics.total_return_pct);
		const sharpe = toNumber(held?.metrics.sharpe);
		const trades = toNumber(held?.metrics.total_trades ?? held?.metrics.trades);
		rows.push({
			key: 'held_back',
			label: 'Held-back test',
			question: 'Does it work on data sealed away from research?',
			value: ret === null ? '—' : `${ret >= 0 ? '+' : '−'}${pctText(Math.abs(ret) * 100, 1)} · Sharpe ${sharpe === null ? '—' : numText(sharpe)}`,
			bullet: null,
			thresholdText: 'one shot per configuration',
			verdict,
			tone: verdict === 'PASS' ? 'ok' : verdict === 'FAIL' ? 'fail' : 'idle',
			evidence: held?.start ? `${trades ?? '—'} trades · ${fmtDateUtc(held.start)} – ${fmtDateUtc(held.end)}` : 'Not run yet',
			weak: false,
			stale: false,
			resultId: evidence.holdout?.result_id ?? null,
			testKey: null,
		});
	}

	// Baseline hurdle: alpha over buy & hold and a trend rule, walk-forward days.
	{
		const baseline = readWalkForward(payload('walk_forward'))?.baseline ?? null;
		const heldBaseline = readHeldBack(evidence.holdout)?.baseline ?? null;
		const t = baseline?.alphaT ?? null;
		const weak = t !== null && t < ALPHA_T_BAR;
		const status = (baseline?.status ?? '').toLowerCase();
		const verdict = !baseline ? 'NOT RUN' : status === 'pass' ? 'PASS' : status === 'fail' ? 'FAIL' : status.toUpperCase() || 'NOT RUN';
		rows.push({
			key: 'baseline',
			label: 'Beats baselines',
			question: 'Is the return more than market or trend exposure?',
			value: baseline && isNum(baseline.alphaPct) ? `alpha ${baseline.alphaPct >= 0 ? '+' : '−'}${Math.abs(baseline.alphaPct).toFixed(1)}%/yr · t ${t === null ? '—' : numText(t)}` : '—',
			bullet: t === null ? null : { min: -1, max: 4, value: t, threshold: ALPHA_T_BAR, direction: 'ge' },
			thresholdText: `t ≥ ${ALPHA_T_BAR} for significance`,
			verdict,
			tone: toneFor(verdict, weak, false),
			evidence: heldBaseline && isNum(heldBaseline.alphaT) ? `held-back alpha t ${numText(heldBaseline.alphaT)}` : 'walk-forward days only',
			weak,
			stale: false,
			resultId: null,
			testKey: null,
		});
	}

	// Deflated Sharpe: advisory probability the edge beats luck across trials.
	{
		const dsr = toNumber(evidence.gauntlet?.deflated_sharpe?.dsr);
		const trials = toNumber(evidence.gauntlet?.deflated_sharpe?.n_trials);
		const verdict = dsr === null ? 'NOT RUN' : dsr >= DSR_BAR ? 'PASS' : 'LOW';
		rows.push({
			key: 'deflated_sharpe',
			label: 'Deflated Sharpe',
			question: 'Is the Sharpe more than luck across all variants tried?',
			value: dsr === null ? '—' : numText(dsr),
			bullet: dsr === null ? null : { min: 0, max: 1, value: dsr, threshold: DSR_BAR, direction: 'ge' },
			thresholdText: `≥ ${DSR_BAR} conventional · advisory`,
			verdict,
			tone: toneFor(verdict, false, false),
			evidence: trials === null ? 'trial count not recorded' : `${trials.toLocaleString('en-US')} trials counted`,
			weak: dsr !== null && dsr < DSR_BAR,
			stale: false,
			resultId: null,
			testKey: null,
		});
	}

	return rows.map((row) => ({ ...row, basis: row.evidence.startsWith(STALE_NOTE) ? row.evidence.slice(STALE_NOTE.length) : row.evidence }));
}

/** The short verdict label a stress row shows. */
export function stressVerdictText(row: StressRow): string {
	if (row.tone === 'idle') return row.verdict === 'NOT RUN' ? 'Not run' : row.verdict.charAt(0) + row.verdict.slice(1).toLowerCase();
	if (row.tone === 'fail') return 'Fail';
	if (row.tone === 'caution') return row.verdict === 'LOW' ? 'Low' : row.stale ? 'Pass · stale' : 'Pass · thin';
	return row.verdict === 'PASS*' ? 'Pass*' : 'Pass';
}
