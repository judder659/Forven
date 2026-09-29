// The Summary verdict: written rules over the container's evidence, each finding
// linked to the tab that holds its proof, and a headline composed from the findings.

import type { Baseline, HeldBack, StressRow, WalkForwardEvidence } from './evidence';
import { ALPHA_T_BAR, DSR_BAR } from './evidence';
import type { GateEta } from './lifecycle';
import type { Concentration, ExitGroup, Slice } from './metrics';
import { fmtDateUtc, fmtFraction, fmtMonthYear, fmtNum, fmtPct, fmtUsd, isNum, toNumber } from './format';

export type FindingTone = 'ok' | 'caution' | 'fail' | 'info';

export interface Finding {
	key: string;
	tone: FindingTone;
	title: string;
	body: string;
	/** Lower-case clause for the headline. */
	short: string;
	/** The rule, shown with the finding. */
	rule: string;
	/** Tab and optional element id holding the evidence. */
	target: { tab: string; anchor?: string };
}

export interface FindingsInput {
	inSample: Slice | null;
	outOfSample: Slice | null;
	heldBack: HeldBack | null;
	walkForward: WalkForwardEvidence | null;
	stressRows: StressRow[];
	dsr: number | null;
	concentration: Concentration | null;
	exits: ExitGroup[];
	tradeCount: number;
	stage: string;
	gate: GateEta | null;
	paperNeed: { days: number | null; trades: number | null };
}

/** A held-back Sharpe below this share of the OOS Sharpe reads as decay. */
export const DECAY_RATIO = 0.6;
/** Top-5 trades making more than this share of net profit reads as concentrated. */
export const CONCENTRATION_SHARE = 50;

export function buildFindings(input: FindingsInput): Finding[] {
	const out: Finding[] = [];
	const oos = input.outOfSample;
	const held = input.heldBack;
	const heldSharpe = toNumber(held?.metrics.sharpe);
	const heldReturn = toNumber(held?.metrics.total_return_pct);
	const heldBaseline: Baseline | null = held?.baseline ?? null;
	const wfBaseline: Baseline | null = input.walkForward?.baseline ?? null;

	// 1. Unseen data: out-of-sample and the held-back test.
	if (oos && isNum(oos.totalReturn)) {
		if (oos.totalReturn <= 0) {
			out.push({
				key: 'oos-loss', tone: 'fail', title: 'Loses money out of sample.',
				body: `The out-of-sample slice returned ${fmtFraction(oos.totalReturn)} (Sharpe ${fmtNum(oos.sharpe)}).`,
				short: 'it loses money out of sample', rule: 'out-of-sample return > 0', target: { tab: 'performance' },
			});
		} else if (held?.verdict === 'FAIL') {
			out.push({
				key: 'held-fail', tone: 'fail', title: 'Failed the held-back test.',
				body: `Profitable out of sample (${fmtFraction(oos.totalReturn)}), but the one-shot test on sealed data failed${isNum(heldReturn) ? ` at ${fmtFraction(heldReturn)}` : ''}.`,
				short: 'it failed the held-back test', rule: 'held-back test PASS', target: { tab: 'robustness', anchor: 'rb-holdout' },
			});
		} else {
			const heldText = held?.verdict === 'PASS' && isNum(heldReturn)
				? ` Held-back ${fmtFraction(heldReturn)} (Sharpe ${fmtNum(heldSharpe)})${heldBaseline && isNum(heldBaseline.returns.buyHold) ? ` while buy & hold made ${fmtPct(heldBaseline.returns.buyHold)}` : ''}.`
				: '';
			out.push({
				key: 'unseen', tone: 'ok', title: 'Profitable on data it was not built on.',
				body: `Out-of-sample ${fmtFraction(oos.totalReturn)} (Sharpe ${fmtNum(oos.sharpe)}, ${fmtMonthYear(oos.start)}–${fmtMonthYear(oos.end)}).${heldText}`,
				short: held?.verdict === 'PASS' ? 'profitable out of sample and on held-back data' : 'profitable out of sample',
				rule: 'out-of-sample return > 0 and held-back test not failed', target: { tab: 'robustness', anchor: 'rb-holdout' },
			});
		}
	}

	// 2. Statistical confidence.
	const wfT = wfBaseline?.alphaT ?? null;
	const heldT = heldBaseline?.alphaT ?? null;
	if ((isNum(input.dsr) && input.dsr < DSR_BAR) || (isNum(wfT) && wfT < ALPHA_T_BAR)) {
		const parts = [
			isNum(input.dsr) ? `Deflated Sharpe ${fmtNum(input.dsr)}: a ${fmtPct(input.dsr * 100, 0, false)} chance the edge is real after selection bias (${DSR_BAR} is the usual bar).` : null,
			isNum(wfT) ? `Alpha over buy & hold and trend has t = ${fmtNum(wfT)} walk-forward${isNum(heldT) ? ` and ${fmtNum(heldT)} held-back` : ''} (about ${ALPHA_T_BAR} is significant).` : null,
		].filter(Boolean);
		out.push({
			key: 'confidence', tone: 'caution', title: 'The edge is not statistically confirmed.',
			body: parts.join(' '), short: 'the edge is not statistically confirmed',
			rule: `deflated Sharpe < ${DSR_BAR} or alpha t < ${ALPHA_T_BAR}`, target: { tab: 'robustness', anchor: 'rb-dsr' },
		});
	}

	// 3. Decay from the best window to the held-back data.
	if (oos && isNum(oos.sharpe) && oos.sharpe > 0 && isNum(heldSharpe) && heldSharpe < DECAY_RATIO * oos.sharpe) {
		const is = input.inSample;
		out.push({
			key: 'decay', tone: 'caution', title: 'Sharpe falls outside its best window.',
			body: `${is && isNum(is.sharpe) ? `${fmtNum(is.sharpe)} in-sample (${fmtMonthYear(is.start)}–${fmtMonthYear(is.end)}) → ` : ''}${fmtNum(oos.sharpe)} out-of-sample → ${fmtNum(heldSharpe)} on the held-back data. Plan paper expectations around the held-back figure.`,
			short: 'its Sharpe falls outside its best window',
			rule: `held-back Sharpe < ${Math.round(DECAY_RATIO * 100)}% of the out-of-sample Sharpe`, target: { tab: 'summary', anchor: 'evidence-ladder' },
		});
	}

	// 4. Profit concentration.
	if (input.concentration && input.concentration.share > CONCENTRATION_SHARE) {
		const c = input.concentration;
		const stops = input.exits.find((group) => group.reason === 'stop_loss');
		out.push({
			key: 'concentration', tone: 'caution', title: 'Profit rides on a few trades.',
			body: `The ${c.top} best of ${input.tradeCount} out-of-sample trades made ${fmtPct(c.share, 0, false)} of net profit (${fmtUsd(c.topSum, 0, false)} of ${fmtUsd(c.net, 0, false)}).${stops && stops.pnl < 0 ? ` ${stops.count} stop-outs cost ${fmtUsd(Math.abs(stops.pnl), 0, false)}.` : ''} Expect long flat stretches between winners.`,
			short: `${Math.round(c.share)}% of the profit came from ${c.top} trades`,
			rule: `top ${c.top} trades > ${CONCENTRATION_SHARE}% of net profit`, target: { tab: 'summary', anchor: 'attribution' },
		});
	}

	// 5. Evidence quality behind passing verdicts.
	const thin = input.stressRows.filter((row) => (row.weak || row.stale) && row.key !== 'deflated_sharpe' && row.key !== 'baseline' && row.verdict !== 'FAIL');
	if (thin.length) {
		out.push({
			key: 'thin', tone: 'caution', title: 'Some robustness verdicts rest on little evidence.',
			body: thin.map((row) => `${row.label}: ${row.evidence}.`).join(' '),
			short: 'some robustness verdicts rest on little evidence',
			rule: 'passed on < 10 jitter reruns, folds under 20 trades, a fold rescue, or stale parameters', target: { tab: 'robustness', anchor: 'stress-matrix' },
		});
	}

	// 6. Failed gates (anything the gate itself rejected).
	for (const row of input.stressRows.filter((item) => item.tone === 'fail' && item.key !== 'held_back')) {
		out.push({
			key: `fail-${row.key}`, tone: 'fail', title: `${row.label} failed.`,
			body: `${row.value}${row.thresholdText ? ` against ${row.thresholdText}` : ''}. ${row.evidence}.`,
			short: `it fails ${row.label.toLowerCase()}`, rule: `${row.label} gate`, target: { tab: 'robustness', anchor: 'stress-matrix' },
		});
	}

	// 7. Costs survived.
	const cost = input.stressRows.find((row) => row.key === 'cost_stress');
	if (cost && cost.tone === 'ok') {
		const d = cost.detail ?? {};
		const multiplier = isNum(d.multiplier) ? `${fmtNum(d.multiplier, 0)}×` : 'higher';
		out.push({
			key: 'costs', tone: 'ok', title: 'Survives higher costs.',
			body: `At ${multiplier} fees and slippage the Sharpe drops ${fmtPct(d.degradationPct, 1, false)}${isNum(d.thresholdPct) ? ` (limit ${fmtPct(d.thresholdPct, 0, false)})` : ''}.`,
			short: 'it survives higher costs', rule: 'cost stress PASS', target: { tab: 'robustness', anchor: 'rb-cost' },
		});
	}

	// 8. Forward evidence: how far the paper → live gate is.
	if (input.stage === 'paper' && input.gate) {
		out.push({
			key: 'gate', tone: 'info', title: 'Paper → live is months away.',
			body: `The gate needs ${input.paperNeed.days ?? '—'} days and ${input.paperNeed.trades ?? '—'} closed paper trades (${input.gate.remainingTrades} to go). At ${fmtNum(input.gate.rates[0], 1)}–${fmtNum(input.gate.rates[1], 1)} trades a month the earliest is ${fmtDateUtc(input.gate.earliest)} – ${fmtDateUtc(input.gate.latest)}.`,
			short: '', rule: 'paper_trading.min_closed_trades ÷ the backtest trade rate', target: { tab: 'summary', anchor: 'gate-card' },
		});
	}

	return out;
}

export interface Verdict {
	tone: 'ok' | 'caution' | 'fail' | 'idle';
	label: string;
	headline: string;
}

function capitalize(text: string): string {
	return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

function joinClauses(clauses: string[]): string {
	if (clauses.length <= 1) return clauses.join('');
	return `${clauses.slice(0, -1).join(', ')} and ${clauses[clauses.length - 1]}`;
}

/** The label and a one-sentence headline composed from the findings. */
export function summarizeVerdict(findings: Finding[]): Verdict {
	const fails = findings.filter((f) => f.tone === 'fail');
	const cautions = findings.filter((f) => f.tone === 'caution');
	const positives = findings.filter((f) => f.tone === 'ok');
	if (fails.length) {
		return { tone: 'fail', label: 'Not viable', headline: `${capitalize(joinClauses(fails.slice(0, 2).map((f) => f.short)))}.` };
	}
	if (!positives.length && !cautions.length) {
		return { tone: 'idle', label: 'Not enough evidence', headline: 'Run a backtest and the robustness suite to judge this strategy.' };
	}
	const lead = positives[0]?.short;
	const worries = cautions.slice(0, 2).map((f) => f.short);
	if (!cautions.length) {
		return { tone: 'ok', label: 'Viable', headline: `${capitalize(lead ?? 'every check passed')}; every check passed.` };
	}
	const headline = lead ? `${capitalize(lead)}, but ${joinClauses(worries)}.` : `${capitalize(joinClauses(worries))}.`;
	return { tone: 'caution', label: 'Promising · not proven', headline };
}
