/** Trade statistics and the forward-vs-backtest verdict for the trading desk. */
import type { FleetBacktestOos } from '$lib/api/dashboard';
import type { DeskFill, DeskMode } from '$lib/api/desk';
import { fmtPct, num, parseTs } from './format';

export function mean(values: number[]): number | null {
	return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

export function median(values: number[]): number | null {
	if (!values.length) return null;
	const sorted = [...values].sort((a, b) => a - b);
	const mid = sorted.length >> 1;
	return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

function choose(n: number, k: number): number {
	let result = 1;
	for (let i = 1; i <= k; i += 1) result = (result * (n - k + i)) / i;
	return result;
}

/** P(X <= k) for X ~ Binomial(n, p). */
export function binomCdf(k: number, n: number, p: number): number {
	let total = 0;
	for (let i = 0; i <= k; i += 1) total += choose(n, i) * p ** i * (1 - p) ** (n - i);
	return Math.min(1, total);
}

/** P(X <= k) for X ~ Poisson(lambda). */
export function poissonCdf(k: number, lambda: number): number {
	let total = 0;
	let term = Math.exp(-lambda);
	for (let i = 0; i <= k; i += 1) {
		if (i > 0) term *= lambda / i;
		total += term;
	}
	return Math.min(1, total);
}

export interface TradeStats {
	/** Closed trades with a net P&L, oldest first. */
	closed: DeskFill[];
	n: number;
	wins: number;
	losses: number;
	net: number;
	profitFactor: number | null;
	/** Mean net P&L per trade as a percent of the capital it was sized from. */
	avgPctOfCapital: number | null;
	avgHoldHours: number | null;
	costs: number;
	best: number | null;
	worst: number | null;
	failed: number;
	/** Cumulative net P&L after each closed trade, starting at 0. */
	cumulative: number[];
	/** Cumulative net P&L after each closed trade, with the close time. */
	timeline: Array<{ time: number; cum: number }>;
	/** Closed trades that recorded the capital slice they were sized from, and those slices summed. */
	sliced: number;
	sliceSum: number;
	/** The earliest entry among fills that did not fail. */
	firstOpened: number | null;
}

export function statsFor(fills: DeskFill[]): TradeStats {
	const closed = fills
		.filter((fill) => String(fill.status).toUpperCase() === 'CLOSED' && num(fill.net_pnl_usd) !== null)
		.sort((a, b) => (parseTs(a.closed_at) ?? 0) - (parseTs(b.closed_at) ?? 0));
	const nets = closed.map((fill) => Number(fill.net_pnl_usd));
	const gains = nets.filter((v) => v > 0);
	const losses = nets.filter((v) => v < 0);
	const grossProfit = gains.reduce((a, b) => a + b, 0);
	const grossLoss = losses.reduce((a, b) => a + b, 0);
	const pctOfCapital = closed
		.filter((fill) => (num(fill.slice_usd) ?? 0) > 0)
		.map((fill) => (Number(fill.net_pnl_usd) / Number(fill.slice_usd)) * 100);
	const holds = closed
		.map((fill) => {
			const opened = parseTs(fill.opened_at);
			const shut = parseTs(fill.closed_at);
			return opened !== null && shut !== null ? (shut - opened) / 3_600_000 : null;
		})
		.filter((value): value is number => value !== null);
	const cumulative = [0];
	for (const value of nets) cumulative.push(cumulative[cumulative.length - 1] + value);
	const timeline = closed.map((fill, index) => ({ time: parseTs(fill.closed_at) ?? 0, cum: cumulative[index + 1] }));
	const slices = closed.map((fill) => num(fill.slice_usd) ?? 0).filter((slice) => slice > 0);
	const opens = fills
		.filter((fill) => String(fill.status).toUpperCase() !== 'FAILED')
		.map((fill) => parseTs(fill.opened_at))
		.filter((value): value is number => value !== null);
	return {
		closed,
		n: closed.length,
		wins: gains.length,
		losses: losses.length,
		net: grossProfit + grossLoss,
		profitFactor: grossLoss < 0 ? grossProfit / Math.abs(grossLoss) : null,
		avgPctOfCapital: mean(pctOfCapital),
		avgHoldHours: mean(holds),
		costs: closed.reduce((sum, fill) => sum + (num(fill.costs_usd) ?? 0), 0),
		best: nets.length ? Math.max(...nets) : null,
		worst: nets.length ? Math.min(...nets) : null,
		failed: fills.filter((fill) => String(fill.status).toUpperCase() === 'FAILED').length,
		cumulative,
		timeline,
		sliced: slices.length,
		sliceSum: slices.reduce((sum, slice) => sum + slice, 0),
		firstOpened: opens.length ? Math.min(...opens) : null,
	};
}

export type Severity = 'fail' | 'caution' | 'info';

export interface Expectation {
	backtest: FleetBacktestOos;
	months: number | null;
	/** Backtest trades per month. */
	rate: number;
	expectedTrades: number | null;
	n: number;
	wins: number;
	expectedWins: number | null;
	/** Chance of this few wins or fewer if the backtest win rate held. */
	pWins: number | null;
	/** Chance of this few trades or fewer at the backtest's pace. */
	pFewerTrades: number | null;
	slow: boolean;
	headline: string;
	verdict: string;
	pill: { tone: 'ok' | 'caution' | 'fail' | 'idle'; text: string };
	flag: Severity | null;
	odds: number | null;
	oddsText: string;
}

const MONTH_MS = 30.44 * 86_400_000;

/**
 * Compare forward results with the out-of-sample backtest. Small samples never
 * convict: under ten trades the verdict says so and reports the odds instead.
 */
export function expectation(input: {
	backtest: FleetBacktestOos | null | undefined;
	stats: TradeStats;
	since: number | null;
	now: number;
	refusedEntries?: number;
	mode: DeskMode;
}): Expectation | null {
	const bt = input.backtest;
	const btTrades = num(bt?.total_trades);
	const btMonths = num(bt?.backtest_months);
	if (!bt || !btTrades || !btMonths) return null;
	const noun = input.mode === 'live' ? 'live' : 'paper';
	const months = input.since !== null ? Math.max(0, (input.now - input.since) / MONTH_MS) : null;
	const rate = btTrades / btMonths;
	const expectedTrades = months !== null ? rate * months : null;
	const { n, wins } = input.stats;
	const pFewerTrades = expectedTrades !== null && n < expectedTrades ? poissonCdf(n, expectedTrades) : null;
	const slow = pFewerTrades !== null && pFewerTrades < 0.05;
	const base = {
		backtest: bt,
		months,
		rate,
		expectedTrades,
		n,
		wins,
		pFewerTrades,
		slow,
	};
	const slowText = slow && months
		? ` It also trades far less often than the backtest: ${n} trades where about ${Math.round(expectedTrades ?? 0)} were expected by now (${(n / months).toFixed(1)} a month against ${rate.toFixed(1)}). Refused entries explain part of a gap like this; check Why.`
		: '';

	if (n === 0) {
		const refused = input.refusedEntries ?? 0;
		return {
			...base,
			expectedWins: null,
			pWins: null,
			headline: `no ${noun} trades yet`,
			verdict: `No closed ${noun} trades in ${months !== null ? months.toFixed(1) : '—'} months. The backtest trades about ${rate.toFixed(1)} times a month, so about ${Math.round(expectedTrades ?? 0)} were expected by now.${refused ? ` ${refused} entries were refused in the last 30 days (see Why).` : ''}`,
			pill: { tone: pFewerTrades !== null && pFewerTrades < 0.01 ? 'caution' : 'idle', text: `No ${noun} trades` },
			flag: null,
			odds: expectedTrades !== null ? poissonCdf(0, expectedTrades) : null,
			oddsText: expectedTrades !== null ? `Chance of zero trades by now at the backtest's pace: ${formatOdds(poissonCdf(0, expectedTrades))}` : '',
		};
	}

	const winRate = num(bt.win_rate) ?? 0;
	const expectedWins = n * winRate;
	const pWins = binomCdf(wins, n, winRate);
	const small = n < 10;
	const worse = wins < expectedWins;
	const oddsText = `Chance of this few wins or fewer if the backtest's win rate held: ${formatOdds(pWins)}`;
	if (!worse) {
		return {
			...base,
			expectedWins,
			pWins,
			headline: slow ? 'trading far less than the backtest' : 'tracking the backtest',
			verdict: `${wins} win${wins === 1 ? '' : 's'} in ${n} ${noun} trades, about ${expectedWins.toFixed(1)} expected from the backtest's ${fmtPct(winRate * 100, 0, false)} win rate, so the wins are in line so far.${slowText}`,
			pill: slow ? { tone: 'caution', text: 'Trading less than expected' } : { tone: 'ok', text: small ? 'On track, small sample' : 'On track' },
			flag: null,
			odds: pWins,
			oddsText,
		};
	}
	if (small) {
		return {
			...base,
			expectedWins,
			pWins,
			headline: pWins < 0.1 ? 'unlucky so far, too early to judge' : 'behind the backtest, too early to judge',
			verdict: `Too early to judge: ${n} closed trades. ${wins} win${wins === 1 ? '' : 's'} where the backtest predicts about ${expectedWins.toFixed(1)}. A run this poor or worse happens about ${fmtPct(pWins * 100, 0, false)} of the time by chance.${slowText}`,
			pill: slow ? { tone: 'caution', text: 'Trading less than expected' } : { tone: pWins < 0.1 ? 'caution' : 'idle', text: 'Too early to judge' },
			flag: pWins < 0.1 ? 'info' : null,
			odds: pWins,
			oddsText,
		};
	}
	return {
		...base,
		expectedWins,
		pWins,
		headline: pWins < 0.05 ? 'significantly worse than the backtest' : 'behind the backtest',
		verdict: `${wins} wins in ${n} trades against about ${expectedWins.toFixed(1)} expected. A result this poor happens ${fmtPct(pWins * 100, 1, false)} of the time by chance${pWins < 0.05 ? ', so the edge may not be holding' : ''}.${slowText}`,
		pill: { tone: pWins < 0.05 ? 'fail' : 'caution', text: pWins < 0.05 ? 'Diverging' : 'Behind' },
		flag: pWins < 0.05 ? 'caution' : pWins < 0.1 ? 'info' : null,
		odds: pWins,
		oddsText,
	};
}

function formatOdds(p: number): string {
	return p < 0.001 ? 'under 0.1%' : fmtPct(p * 100, 1, false);
}
