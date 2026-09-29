/**
 * Manual order ticket math. Mirrors the backend's sizing so the preview matches
 * the order that will be sent:
 * - live: units = equity × risk ÷ |price − stop|, capped at equity × leverage
 *   (exchange/risk.calculate_position_size). A stop is required.
 * - paper: the shared sizing mirror on the strategy's own book: fraction =
 *   min(1, risk ÷ (stop distance × leverage)), units = book × leverage × fraction
 *   ÷ price. Without a stop the fraction is the risk itself.
 */
import type { DeskMode } from '$lib/api/desk';
import { fmtPct, fmtPx, fmtUsd, num } from './format';

export type SizeMode = 'risk' | 'usd' | 'units';
export type CheckTone = 'ok' | 'caution' | 'fail';

export interface TicketInput {
	mode: DeskMode;
	direction: 'long' | 'short';
	sizeMode: SizeMode;
	riskPct: unknown;
	usd: unknown;
	units: unknown;
	leverage: unknown;
	stop: unknown;
	takeProfit: unknown;
	price: number | null;
	/** Live: account equity. Paper: the strategy's own book. */
	equity: number | null;
	takerBps: number;
	strategyId: string;
	ceilingUsd?: number | null;
	wallet?: { name: string; freeMargin: number | null } | null;
	limits?: {
		perTradeRiskPct: number;
		orderNotionalPct: number;
		openRiskUsed: number;
		openRiskLimit: number | null;
	};
	gatesBlocking?: string[];
}

export interface TicketCheck {
	tone: CheckTone;
	text: string;
}

export interface TicketResult {
	errors: string[];
	checks: TicketCheck[];
	size: number | null;
	notional: number | null;
	margin: number | null;
	riskUsd: number | null;
	riskPctOfEquity: number | null;
	feeUsd: number | null;
	stopPct: number | null;
	rr: number | null;
	leverage: number;
	blocked: boolean;
}

export function ticketCalc(input: TicketInput): TicketResult {
	const leverage = Math.max(1, num(input.leverage) ?? 1);
	const result: TicketResult = {
		errors: [],
		checks: [],
		size: null,
		notional: null,
		margin: null,
		riskUsd: null,
		riskPctOfEquity: null,
		feeUsd: null,
		stopPct: null,
		rr: null,
		leverage,
		blocked: true,
	};
	const price = num(input.price);
	const equity = num(input.equity);
	if (price === null || price <= 0) {
		result.errors.push('No current price for this market yet.');
		return result;
	}
	if (equity === null || equity <= 0) {
		result.errors.push(input.mode === 'live' ? 'The live balance has not synced yet.' : 'This paper book has no capital.');
		return result;
	}
	const stop = num(input.stop);
	const hasStop = stop !== null && stop > 0;
	if (input.mode === 'live' && !hasStop) {
		result.errors.push('Set a stop. Forven refuses a live open without a protective stop.');
		return result;
	}
	if (hasStop && input.direction === 'long' && (stop as number) >= price) {
		result.errors.push(`A long's stop has to sit below the price (${fmtPx(price)}).`);
		return result;
	}
	if (hasStop && input.direction === 'short' && (stop as number) <= price) {
		result.errors.push(`A short's stop has to sit above the price (${fmtPx(price)}).`);
		return result;
	}
	const distance = hasStop ? Math.abs(price - (stop as number)) : null;
	const stopFrac = distance !== null ? distance / price : null;

	let size: number | null = null;
	if (input.sizeMode === 'risk') {
		const risk = num(input.riskPct);
		if (risk === null || risk <= 0 || risk > 100) {
			result.errors.push('Enter a risk between 0 and 100% of the book.');
			return result;
		}
		if (input.mode === 'live') {
			size = (equity * (risk / 100)) / (distance as number);
			if (size * price > equity * leverage) size = (equity * leverage) / price;
		} else {
			const fraction = Math.min(1, stopFrac ? risk / 100 / (stopFrac * leverage) : risk / 100);
			size = (equity * leverage * fraction) / price;
		}
	} else if (input.sizeMode === 'usd') {
		const usd = num(input.usd);
		size = usd !== null && usd > 0 ? usd / price : null;
	} else {
		const units = num(input.units);
		size = units !== null && units > 0 ? units : null;
	}
	if (size === null || !(size > 0)) {
		result.errors.push('Enter a size above zero.');
		return result;
	}

	const notional = size * price;
	result.size = size;
	result.notional = notional;
	result.margin = notional / leverage;
	result.riskUsd = distance !== null ? size * distance : null;
	result.riskPctOfEquity = result.riskUsd !== null ? (result.riskUsd / equity) * 100 : null;
	result.feeUsd = (notional * input.takerBps) / 10_000;
	result.stopPct = stopFrac !== null ? stopFrac * 100 : null;

	const takeProfit = num(input.takeProfit);
	if (takeProfit !== null && takeProfit > 0) {
		const rightSide = input.direction === 'long' ? takeProfit > price : takeProfit < price;
		if (!rightSide) {
			result.checks.push({ tone: 'fail', text: `A ${input.direction}'s target has to sit ${input.direction === 'long' ? 'above' : 'below'} the price` });
		} else if (distance !== null) {
			result.rr = Math.abs(takeProfit - price) / distance;
		}
	}

	if (input.mode === 'live') {
		const limits = input.limits;
		const riskUsd = result.riskUsd ?? 0;
		if (limits) {
			const perTradeCap = (equity * limits.perTradeRiskPct) / 100;
			result.checks.push(riskUsd <= perTradeCap
				? { tone: 'ok', text: `Risk ${fmtUsd(riskUsd)} is within the ${fmtPct(limits.perTradeRiskPct, 0, false)} per-trade cap (${fmtUsd(perTradeCap)})` }
				: { tone: 'fail', text: `Risk ${fmtUsd(riskUsd)} is above the per-trade cap of ${fmtUsd(perTradeCap)}` });
			if (limits.openRiskLimit !== null) {
				const left = limits.openRiskLimit - limits.openRiskUsed;
				result.checks.push(riskUsd <= left
					? { tone: 'ok', text: `Open-risk budget: ${fmtUsd(left - riskUsd)} of ${fmtUsd(limits.openRiskLimit)} left after this order` }
					: { tone: 'fail', text: `Over the open-risk budget: ${fmtUsd(left)} left, this order risks ${fmtUsd(riskUsd)}` });
			}
			const maxOrder = (equity * limits.orderNotionalPct) / 100;
			result.checks.push(notional <= maxOrder
				? { tone: 'ok', text: `Order value ${fmtUsd(notional)} is under the ${fmtUsd(maxOrder)} single-order cap` }
				: { tone: 'fail', text: `Order value ${fmtUsd(notional)} is over the ${fmtUsd(maxOrder)} single-order cap` });
		}
		if (input.wallet) {
			const free = input.wallet.freeMargin;
			if (free !== null) {
				result.checks.push((result.margin ?? 0) <= free
					? { tone: 'ok', text: `Routes to the ${input.wallet.name} wallet: ${fmtUsd(result.margin)} margin of ${fmtUsd(free)} free` }
					: { tone: 'fail', text: `The ${input.wallet.name} wallet has ${fmtUsd(free)} margin free; this needs ${fmtUsd(result.margin)}` });
			}
		}
		const ceiling = num(input.ceilingUsd);
		result.checks.push(ceiling !== null
			? notional <= ceiling
				? { tone: 'ok', text: `Under ${input.strategyId}'s ${fmtUsd(ceiling)} ceiling` }
				: { tone: 'fail', text: `Over ${input.strategyId}'s ${fmtUsd(ceiling)} ceiling. Refused, not downsized.` }
			: { tone: 'caution', text: `${input.strategyId} has no notional ceiling; only the limits above apply` });
		const gates = input.gatesBlocking ?? [];
		result.checks.push(gates.length
			? { tone: 'fail', text: `Blocked by ${gates.join(', ')}` }
			: { tone: 'ok', text: 'Kill switch off, daily loss clear, exchange APIs healthy' });
	} else {
		if (!hasStop) {
			result.checks.push({ tone: 'caution', text: 'No stop: the position can run until the strategy or you close it' });
		}
		result.checks.push({ tone: 'ok', text: 'Paper: fills at the current mid in the simulated book; no real order is sent' });
	}
	result.blocked = result.checks.some((check) => check.tone === 'fail');
	return result;
}

/** A sensible default risk: the strategy's own per-trade risk on its capital, as a percent of `equity`. */
export function defaultRiskPct(mode: DeskMode, riskPerTrade: number | null, sliceUsd: number | null, equity: number | null): number {
	const perTrade = riskPerTrade && riskPerTrade > 0 ? riskPerTrade : 0.01;
	if (mode === 'paper') return +(perTrade * 100).toFixed(2);
	if (sliceUsd && equity && equity > 0) return +(((perTrade * sliceUsd) / equity) * 100).toFixed(2);
	return +(perTrade * 100).toFixed(2);
}
