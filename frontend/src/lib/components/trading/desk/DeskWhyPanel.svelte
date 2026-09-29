<script lang="ts">
	/** Why a strategy did or didn't trade: its signal, scanning, regime, gates, room, ceiling, refusals. */
	import type { ForvenDashboardResponse, ForvenRiskStatus } from '$lib/api';
	import type { LiveFleet } from '$lib/api/dashboard';
	import type { DeskMode, JournalEvent } from '$lib/api/desk';
	import { describeRefusal, humanRegime } from '$lib/utils/tradingDesk/describe';
	import { ago, cap1, fmtDateTime, fmtDay, fmtNum, fmtPct, fmtTime, fmtUsd, lastBarClose, num } from '$lib/utils/tradingDesk/format';
	import { blockingGates } from '$lib/utils/tradingDesk/gates';
	import type { DeskRow } from '$lib/utils/tradingDesk/rows';
	import ExecutionCheckPanel from '$lib/components/strategy/ExecutionCheckPanel.svelte';
	import type { ExecutionCheck } from '$lib/api/executionCheck';

	export let mode: DeskMode;
	export let row: DeskRow;
	export let dashboard: ForvenDashboardResponse | null = null;
	export let risk: ForvenRiskStatus | null = null;
	export let fleet: LiveFleet | null = null;
	export let refusals: JournalEvent[] = [];
	export let now = Date.now();

	type Tone = 'ok' | 'caution' | 'fail' | 'info';
	interface Item { tone: Tone; title: string; body: string; list?: Array<{ when: string; text: string }> }

	const HIDDEN_INDICATORS = new Set(['price', 'live_price', 'entry_signal', 'exit_signal']);

	$: recentRefusals = refusals.filter((event) => event.kind === 'entry_refused' || event.kind === 'exit_refused');
	$: sessionBlocked = row.session.status === 'blocked' && Boolean(row.session.blocked_reason);
	// A fix clears the check at once; the session's own flag waits for the scanner's next pass.
	let fixedFor: string | null = null;
	$: if (fixedFor && fixedFor !== row.sid) fixedFor = null;
	function onChecked(event: CustomEvent<ExecutionCheck>): void {
		if (event.detail.executable) fixedFor = row.sid;
	}
	$: items = buildItems(mode, row, dashboard, risk, fleet, recentRefusals, now);

	function buildItems(
		mode: DeskMode,
		row: DeskRow,
		dashboard: ForvenDashboardResponse | null,
		risk: ForvenRiskStatus | null,
		fleet: LiveFleet | null,
		recentRefusals: JournalEvent[],
		now: number
	): Item[] {
		const session = row.session;
		const indicators = session.indicators ?? {};
		const entrySignal = num(indicators.entry_signal?.value) ?? 0;
		const exitSignal = num(indicators.exit_signal?.value) ?? 0;
		const extras = Object.entries(indicators).filter(([key]) => !HIDDEN_INDICATORS.has(key));
		const pending = session.pending_signals ?? [];
		const sides = session.trade_mode === 'short_only' ? ['short'] : session.trade_mode === 'long_only' ? ['long'] : ['long', 'short'];
		const gate = risk?.regime_gate ?? null;
		const stance = gate?.stances?.find((entry) => entry.asset === row.asset) ?? null;
		const blocking = blockingGates(dashboard);
		const refusalCount = recentRefusals.reduce((sum, event) => sum + (event.count ?? 1), 0);
		const weekRefusals = recentRefusals.filter((event) => now - Date.parse(event.at) < 7 * 86_400_000);
		const list: Item[] = [];
		list.push({
			tone: pending.length || entrySignal > 0 ? 'caution' : 'info',
			title: entrySignal > 0 ? 'Entry signal on the last closed bar' : pending.length ? 'A signal is getting close' : 'No entry signal on the last closed bar',
			body: `The ${row.timeframe} bar that closed at ${fmtTime(lastBarClose(row.timeframe, now))} read entry ${fmtNum(entrySignal, 0)} · exit ${fmtNum(exitSignal, 0)}.${pending.length ? '' : ' The strategy reports no condition close to triggering.'}${
				pending.length ? ` ${pending.map((signal) => `${signal.description}: ${fmtNum(signal.current_value)} → ${fmtNum(signal.trigger_value)} (${fmtPct(signal.distance_pct, 1, false)} away)`).join('; ')}.` : ''
			}${extras.length ? ` Values now: ${extras.map(([key, value]) => `${key.replace(/_/g, ' ')} ${fmtNum(value?.value, Math.abs(num(value?.value) ?? 0) >= 1000 ? 1 : 2)}`).join(' · ')}.` : ''}`,
		});
		const scanAt = row.fleet?.last_scan?.at ?? null;
		const staleMinutes = Math.round((fleet?.stale_after_seconds ?? 1800) / 60);
		list.push({
			tone: row.state === 'stale' ? 'fail' : 'ok',
			title: row.state === 'stale' ? 'Not being evaluated' : 'Evaluated on schedule',
			body: scanAt ? `Last scanner check ${ago(scanAt, now)}. Marked stale after ${staleMinutes} minutes without a check.` : 'No scanner check recorded yet.',
		});
		if (stance && gate) {
			const minConf = gate.min_confidence ?? 0.6;
			const confidence = num(stance.confidence) ?? 0;
			const listedLong = gate.block_long?.includes(String(stance.regime)) && sides.includes('long');
			const listedShort = gate.block_short?.includes(String(stance.regime)) && sides.includes('short');
			const listed = listedLong ? 'long' : listedShort ? 'short' : null;
			const acts = Boolean(listed) && confidence >= minConf;
			list.push({
				tone: gate.mode === 'enforce' && acts ? 'fail' : 'ok',
				title: `${row.asset} is ${humanRegime(stance.regime)}`,
				body: `${fmtPct(confidence * 100, 0, false)} confidence, since ${fmtDateTime(stance.since)}. The regime gate is in ${gate.mode} mode${gate.mode === 'observe' ? ': it records trades it would have blocked but never blocks' : ''}. It flags longs in ${(gate.block_long ?? []).map(humanRegime).join(' and ') || 'no regime'}${(gate.block_short ?? []).length ? ` and shorts in ${gate.block_short.map(humanRegime).join(' and ')}` : ''}.${
					listed ? (acts ? ` A ${listed} would be flagged now.` : ` ${cap1(humanRegime(stance.regime))} is on the ${listed} list, but ${fmtPct(confidence * 100, 0, false)} is under the ${fmtPct(minConf * 100, 0, false)} confidence the gate needs, so a ${listed} would not be flagged.`) : ''
				}`,
			});
		}
		if (mode === 'live') {
			list.push({
				tone: blocking.length ? 'fail' : 'ok',
				title: blocking.length ? 'An account gate is closed' : 'Account gates clear',
				body: blocking.length
					? blocking.map((entry) => `${entry.name}: ${entry.value}`).join(' · ')
					: `Kill switch off, daily loss ${fmtPct(Math.abs(Math.min(0, (num(dashboard?.daily_risk?.pnl_pct) ?? 0) * 100)), 2, false)} of the ${fmtPct((num(risk?.limits?.daily_loss_limit) ?? 0.05) * 100, 0, false)} limit, recovery ${dashboard?.recovery?.status ?? 'ok'}, exchange APIs healthy.`,
			});
			const wallets = (fleet?.capacity?.wallets ?? []).filter((wallet) => wallet.sides.some((side) => sides.includes(side)));
			const books = risk?.portfolio_budget_live?.per_book ?? {};
			const slice = risk?.portfolio_budget_live?.capital_slice?.slice_usd ?? fleet?.capacity?.slice_usd ?? null;
			list.push({
				tone: wallets.some((wallet) => wallet.over_capacity) ? 'caution' : 'ok',
				title: 'Room in the wallets it uses',
				body: `${wallets.map((wallet) => {
					const book = books[wallet.wallet];
					const free = (num(book?.limit_usd) ?? num(wallet.capacity_usd) ?? 0) - (num(book?.margin_usd) ?? 0);
					return `${cap1(wallet.wallet)} wallet: ${fmtUsd(free)} margin free of ${fmtUsd(wallet.capacity_usd)}. If every ${wallet.wallet} strategy opened at once: ${fmtUsd(wallet.worst_case_margin_usd)}${wallet.over_capacity ? ', over capacity' : ', fits'}.`;
				}).join(' ')}${slice ? ` Each order is sized from a ${fmtUsd(slice)} slice of the account.` : ''}` || 'Wallet figures are not available yet.',
			});
			const ceiling = num(session.live_notional_ceiling_usd);
			list.push({
				tone: ceiling !== null ? 'ok' : 'caution',
				title: ceiling !== null ? `Ceiling ${fmtUsd(ceiling)}` : 'No notional ceiling',
				body: ceiling !== null ? 'Orders above the ceiling are refused, not downsized.' : 'Nothing caps one order beyond the slice and account limits. Orders above a ceiling are refused, not downsized. Set it in Details.',
			});
		} else {
			list.push({
				tone: 'ok',
				title: 'Paper ignores account halts',
				body: `Paper opens run whatever the kill switch, daily loss or exchange state; they size off this strategy's own book (${fmtUsd(session.capital)} now).`,
			});
		}
		list.push({
			tone: weekRefusals.length ? 'caution' : 'ok',
			title: recentRefusals.length ? `${refusalCount} refusal${refusalCount === 1 ? '' : 's'} in the last 30 days` : 'No refusals in the last 30 days',
			body: recentRefusals.length ? '' : 'Every matched signal in the window was either taken or had nothing to act on.',
			list: recentRefusals.slice(0, 8).map((event) => ({
				when: fmtDay(event.first_at ?? event.at),
				text: `${event.kind === 'entry_refused' ? 'Entry' : 'Exit'} ×${event.count ?? 1}: ${describeRefusal(event.reason).short}${event.kind === 'exit_refused' ? (event.positioned ? ' · position open' : ' · while flat') : ''}`,
			})),
		});
		return list;
	}

	const MARK: Record<Tone, string> = {
		ok: 'bg-[#3cc48f]/15 text-[#3cc48f]',
		caution: 'bg-[#e7b24a]/15 text-[#e7b24a]',
		fail: 'bg-[#e5574f]/15 text-[#e5574f]',
		info: 'bg-sc-raise text-sc-ink2',
	};
	const GLYPH: Record<Tone, string> = { ok: '✓', caution: '!', fail: '!', info: 'i' };
</script>

{#if sessionBlocked}
	<div class="mb-2 grid gap-2">
		<ExecutionCheckPanel
			strategyId={row.sid}
			refreshKey={`${row.session.status}|${row.session.blocked_reason ?? ''}`}
			forgeHref={`/lab/strategy/${encodeURIComponent(row.sid)}`}
			on:changed={onChecked}
		/>
		{#if fixedFor === row.sid}
			<p class="text-[12px] text-[#3cc48f]" data-testid="desk-why-fixed">Execution check passed. The strategy card updates after the scanner's next pass, within a few minutes.</p>
		{/if}
	</div>
{/if}
<ul class="grid" data-testid="desk-why">
	{#each items as item}
		<li class="grid grid-cols-[22px_minmax(0,1fr)] gap-2 border-b border-sc-line py-2 last:border-b-0">
			<span class={`mt-px grid h-[18px] w-[18px] place-items-center rounded-full font-plex-mono text-[11px] font-semibold ${MARK[item.tone]}`}>{GLYPH[item.tone]}</span>
			<div class="min-w-0">
				<div class="text-[13px] font-semibold text-sc-ink">{item.title}</div>
				{#if item.body}<div class="text-[12px] text-sc-ink2">{item.body}</div>{/if}
				{#if item.list?.length}
					<ul class="mt-1 grid gap-1">
						{#each item.list as entry}
							<li class="grid grid-cols-[52px_minmax(0,1fr)] gap-2 text-[12px] text-sc-ink2"><time class="font-plex-mono text-[11px] text-sc-ink3">{entry.when}</time><span>{entry.text}</span></li>
						{/each}
					</ul>
					{#if recentRefusals.length > 8}<span class="text-[11.5px] text-sc-ink3">{recentRefusals.length - 8} more in Decisions.</span>{/if}
				{/if}
			</div>
		</li>
	{/each}
</ul>
