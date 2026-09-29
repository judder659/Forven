/** Exchange-style fill rows (an open fill, then a close fill) and execution quality. */
import type { DeskFill } from '$lib/api/desk';
import { describeClose, type CloseText } from './describe';
import { num, parseTs } from './format';
import { median } from './stats';

export interface FillRow {
	key: string;
	fill: DeskFill;
	kind: 'open' | 'close' | 'failed';
	time: number;
	price: number | null;
	value: number | null;
	/** Signed; positive = filled worse than the signal price. */
	slippageBps: number | null;
	netPnl: number | null;
	why: CloseText;
	/** Buys (open long, close short) vs sells, for the exchange's colour convention. */
	isBuy: boolean;
}

export function fillRows(fills: DeskFill[]): FillRow[] {
	const rows: FillRow[] = [];
	for (const fill of fills) {
		const short = String(fill.direction ?? '').toLowerCase() === 'short';
		const size = num(fill.size) ?? 0;
		const status = String(fill.status ?? '').toUpperCase();
		const opened = parseTs(fill.opened_at);
		if (status === 'FAILED') {
			if (opened !== null) {
				rows.push({
					key: `${fill.id}:failed`,
					fill,
					kind: 'failed',
					time: opened,
					price: num(fill.entry_price),
					value: null,
					slippageBps: null,
					netPnl: null,
					why: { text: fill.failure_reason || 'Order did not fill', tone: '' },
					isBuy: !short,
				});
			}
			continue;
		}
		if (opened !== null) {
			const price = num(fill.entry_price);
			rows.push({
				key: `${fill.id}:open`,
				fill,
				kind: 'open',
				time: opened,
				price,
				value: price !== null ? price * size : null,
				slippageBps: num(fill.entry_slippage_bps),
				netPnl: null,
				why: { text: fill.source === 'manual' ? 'Manual entry' : 'Strategy entry', tone: '' },
				isBuy: !short,
			});
		}
		const closed = parseTs(fill.closed_at);
		if (status === 'CLOSED' && closed !== null) {
			const price = num(fill.exit_price);
			rows.push({
				key: `${fill.id}:close`,
				fill,
				kind: 'close',
				time: closed,
				price,
				value: price !== null ? price * size : null,
				slippageBps: num(fill.exit_slippage_bps),
				netPnl: num(fill.net_pnl_usd),
				why: describeClose({ status, close_reason: fill.close_reason, exit_price: price, stop_price: fill.stop_price }),
				isBuy: short,
			});
		}
	}
	return rows.sort((a, b) => b.time - a.time);
}

export interface ExecutionSummary {
	entryMedian: number | null;
	entryCount: number;
	exitMedian: number | null;
	exitCount: number;
	worstEntry: number | null;
}

/**
 * Median slippage against the signal price over strategy fills. Bot Factory fills
 * (strategy ids starting "bot:") are left out: early bot test orders slipped
 * 100+ bps and would swamp the strategies' own execution.
 */
export function executionSummary(fills: DeskFill[]): ExecutionSummary {
	const strategyFills = fills.filter((fill) => !String(fill.strategy_id ?? '').startsWith('bot:'));
	const entries = strategyFills.map((fill) => num(fill.entry_slippage_bps)).filter((v): v is number => v !== null);
	const exits = strategyFills.map((fill) => num(fill.exit_slippage_bps)).filter((v): v is number => v !== null);
	return {
		entryMedian: median(entries),
		entryCount: entries.length,
		exitMedian: median(exits),
		exitCount: exits.length,
		worstEntry: entries.length ? Math.max(...entries) : null,
	};
}
