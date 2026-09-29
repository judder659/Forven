/** Funding and open-interest helpers for the desk's market strip and position card. */
import type { AssetMarketContext } from '$lib/api/desk';
import { fmtRateHourly, fmtUsd, num } from './format';

/** "longs pay 0.0013%/h" / "shorts pay …" / "no funding". */
export function fundingDirection(rateHourly: number | null | undefined): string {
	const rate = num(rateHourly);
	if (rate === null) return 'no funding data';
	if (rate === 0) return 'no funding either way';
	return `${rate > 0 ? 'longs pay shorts' : 'shorts pay longs'} ${fmtRateHourly(Math.abs(rate))}`;
}

/**
 * Funding for a position over one hour at the current rate, signed from the
 * position's point of view: negative = it pays, positive = it receives.
 */
export function positionFundingPerHour(side: 'long' | 'short', size: number, mark: number, rateHourly: number | null | undefined): number | null {
	const rate = num(rateHourly);
	if (rate === null || !(size > 0) || !(mark > 0)) return null;
	const flow = size * mark * rate;
	return side === 'long' ? -flow : flow;
}

export function fundingFlowText(perHour: number | null): string {
	if (perHour === null) return 'No funding estimate';
	if (Math.abs(perHour) < 0.005) return 'Funding under $0.01 an hour';
	return `${perHour < 0 ? 'Pays' : 'Receives'} about ${fmtUsd(Math.abs(perHour))} an hour in funding`;
}

/** The asset key for a symbol ("ETH/USDT" → "ETH"). */
export function assetOf(symbol: string | null | undefined): string {
	return String(symbol ?? '').trim().toUpperCase().split(/[/:_-]/)[0] ?? '';
}

export function marketFor(
	contexts: Record<string, AssetMarketContext> | null | undefined,
	symbol: string | null | undefined
): AssetMarketContext | null {
	if (!contexts) return null;
	return contexts[assetOf(symbol)] ?? null;
}
