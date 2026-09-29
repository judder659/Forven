/** "Needs attention" items for the trading desk, built from data already on the page. */
import type { ForvenDashboardResponse, ForvenRiskStatus } from '$lib/api';
import type { LiveFleet } from '$lib/api/dashboard';
import type { DeskMode, JournalEvent } from '$lib/api/desk';
import { describeRefusal } from './describe';
import { ago, fmtUsd, parseTs } from './format';
import { blockingGates } from './gates';
import type { Expectation, Severity } from './stats';

export type SideTab = 'position' | 'why' | 'expect' | 'details';

export interface AttentionItem {
	id: string;
	severity: Severity;
	title: string;
	body: string;
	strategyId?: string;
	tab?: SideTab;
}

const WEEK_MS = 7 * 86_400_000;
const ORDER: Record<Severity, number> = { fail: 0, caution: 1, info: 2 };

export function buildAttention(input: {
	mode: DeskMode;
	dashboard: ForvenDashboardResponse | null;
	risk: ForvenRiskStatus | null;
	fleet: LiveFleet | null;
	journal: JournalEvent[];
	expectations: Record<string, Expectation | null>;
	/** Strategy id -> the reason the scanner refuses its new entries right now. */
	sessionBlocks?: Record<string, string>;
	now: number;
}): AttentionItem[] {
	const { mode, dashboard, risk, fleet, now } = input;
	const items: AttentionItem[] = [];
	const noun = mode === 'live' ? 'live' : 'paper';

	// Paper opens ignore every halt, so account gates only matter on the live desk.
	if (mode === 'live') {
		if (dashboard && dashboard.trading_allowed === false) {
			items.push({ id: 'trading', severity: 'fail', title: 'Trading is blocked', body: dashboard.trading_reason || 'A trading gate is closed.' });
		}
		for (const gate of blockingGates(dashboard)) {
			items.push({ id: `gate-${gate.key}`, severity: 'fail', title: `${gate.name}: ${gate.value}`, body: 'New live entries are refused until this clears.' });
		}
		const stopsMissing = risk?.portfolio_budget_live?.stops_missing ?? 0;
		if (stopsMissing > 0) {
			items.push({ id: 'stops-missing', severity: 'fail', title: `${stopsMissing} live position${stopsMissing === 1 ? '' : 's'} without a stop`, body: 'Set a stop on each from its position card.' });
		}
	}

	const flatExitRefusals = new Map<string, JournalEvent[]>();
	for (const event of input.journal) {
		if (event.kind !== 'exit_refused' || event.positioned !== false) continue;
		const at = parseTs(event.at);
		if (at === null || now - at > WEEK_MS) continue;
		const list = flatExitRefusals.get(event.strategy_id) ?? [];
		list.push(event);
		flatExitRefusals.set(event.strategy_id, list);
	}

	for (const strategy of fleet?.strategies ?? []) {
		const sid = strategy.strategy_id;
		if (strategy.state === 'stale') {
			const holding = strategy.open_trade_ids.length > 0;
			items.push({
				id: `stale-${sid}`,
				severity: 'fail',
				strategyId: sid,
				tab: 'why',
				title: `${sid} is not being evaluated${holding ? ' while holding a position' : ''}`,
				body: `No scanner check since ${ago(strategy.last_scan?.at, now)}. An open position cannot exit while this lasts.`,
			});
		}
		if (strategy.state === 'exit_blocked') {
			items.push({
				id: `exit-blocked-${sid}`,
				severity: 'fail',
				strategyId: sid,
				tab: 'position',
				title: `${sid} could not exit an open position`,
				body: `${describeRefusal(strategy.blocked_exits?.last_positioned_reason).short}. Last refusal ${ago(strategy.blocked_exits?.last_positioned_at, now)}. Close it by hand or fix the cause.`,
			});
		}
		const flat = flatExitRefusals.get(sid);
		if (flat?.length && strategy.state !== 'exit_blocked') {
			const count = flat.reduce((sum, event) => sum + (event.count ?? 1), 0);
			items.push({
				id: `flat-exits-${sid}`,
				severity: 'caution',
				strategyId: sid,
				tab: 'why',
				title: `${sid}: ${count} exit signal${count === 1 ? '' : 's'} refused this week while flat`,
				body: `${describeRefusal(flat[0].reason).short}. Harmless while flat; the same fault on an open position would leave it stuck.`,
			});
		}
		const currentBlock = input.sessionBlocks?.[sid];
		if (currentBlock) {
			// A block in force now outranks the refusal history; its Why tab carries the fix.
			items.push({
				id: `blocked-${sid}`,
				severity: 'caution',
				strategyId: sid,
				tab: 'why',
				title: `${sid}: new ${noun} entries are blocked`,
				body: `${describeRefusal(currentBlock).short}. Its Why tab shows what changed and how to fix it.`,
			});
		} else if (strategy.state === 'blocked') {
			const blocked = strategy.blocked_entries;
			items.push({
				id: `blocked-${sid}`,
				severity: 'caution',
				strategyId: sid,
				tab: 'why',
				title: `${sid}: ${blocked.count} entries refused in ${blocked.window_days} days`,
				body: `Most often: ${describeRefusal(blocked.top_reason).short} (${blocked.top_count}×). Last refusal ${ago(blocked.last_at, now)}.`,
			});
		}
		const exp = input.expectations[sid];
		if (exp?.flag) {
			items.push({ id: `expect-${sid}`, severity: exp.flag, strategyId: sid, tab: 'expect', title: `${sid}: ${exp.headline}`, body: exp.verdict });
		}
	}

	if (mode === 'live') {
		const budget = risk?.portfolio_budget_live;
		const missing = budget?.ceilings_missing ?? [];
		const total = fleet?.strategies.length ?? missing.length;
		if (missing.length) {
			const slice = budget?.capital_slice?.slice_usd;
			items.push({
				id: 'ceilings',
				severity: 'caution',
				strategyId: missing[0],
				tab: 'details',
				title: `No go-live ceiling on ${missing.length === total ? `any of the ${total}` : `${missing.length} of the ${total}`} live strategies`,
				body: `Only the ${fmtUsd(slice)} capital slice and the account limits bound an order's size. Set a ceiling per strategy in Details.`,
			});
		}
		const groups = new Map<string, { coin: string; sides: string[]; ids: Set<string> }>();
		for (const conflict of fleet?.capacity?.conflicts ?? []) {
			const key = `${conflict.coin}:${conflict.sides.join('/')}`;
			const group = groups.get(key) ?? { coin: conflict.coin, sides: conflict.sides, ids: new Set<string>() };
			conflict.strategy_ids.forEach((id) => group.ids.add(id));
			groups.set(key, group);
		}
		for (const group of groups.values()) {
			const ids = [...group.ids];
			items.push({
				id: `conflict-${group.coin}-${group.sides.join('')}`,
				severity: 'caution',
				strategyId: ids[0],
				tab: 'why',
				title: `${ids.join(', ')} all trade ${group.coin} ${group.sides.join('/')}`,
				body: 'Hyperliquid nets positions per wallet, so the first of them to enter refuses the others’ entries. Keep one live per coin and side.',
			});
		}
	}

	const slow = Object.entries(input.expectations)
		.filter((entry): entry is [string, Expectation] => Boolean(entry[1]?.slow))
		.sort(([, a], [, b]) => a.n / (a.expectedTrades || 1) - b.n / (b.expectedTrades || 1));
	if (slow.length) {
		items.push({
			id: 'slow',
			severity: 'info',
			strategyId: slow[0][0],
			tab: 'expect',
			title: `${slow.length} of ${fleet?.strategies.length ?? slow.length} strategies trade far less often than their backtests`,
			body: `${cap(noun)} trades against the backtest's pace: ${slow.map(([sid, exp]) => `${sid} ${exp.n} of ~${Math.round(exp.expectedTrades ?? 0)}`).join(' · ')}. Refused entries explain part of it; each strategy's Why tab lists them.`,
		});
	}

	return items.sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);
}

function cap(text: string): string {
	return text.charAt(0).toUpperCase() + text.slice(1);
}
