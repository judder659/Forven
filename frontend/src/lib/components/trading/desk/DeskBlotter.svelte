<script lang="ts" context="module">
	export type BlotTab = 'positions' | 'orders' | 'fills' | 'decisions' | 'performance' | 'risk';
	export type BlotScope = 'strategy' | 'all';
</script>

<script lang="ts">
	/** Positions, resting orders (live) or watched levels (paper), fills, decisions, performance and risk. */
	import { createEventDispatcher } from 'svelte';
	import type { ForvenEquityHistory, ForvenRiskStatus } from '$lib/api';
	import type { LiveFleet } from '$lib/api/dashboard';
	import type { DeskFill, DeskMode, JournalEvent } from '$lib/api/desk';
	import { cap1, dur, fmtPct, fmtPx, fmtQty, fmtUsd, toneClass } from '$lib/utils/tradingDesk/format';
	import type { DeskRow } from '$lib/utils/tradingDesk/rows';
	import DeskFillsTable from './DeskFillsTable.svelte';
	import DeskJournalList from './DeskJournalList.svelte';
	import DeskPerformance from './DeskPerformance.svelte';
	import DeskRiskPanel from './DeskRiskPanel.svelte';

	export let mode: DeskMode;
	export let rows: DeskRow[] = [];
	export let fills: DeskFill[] = [];
	export let journal: JournalEvent[] = [];
	export let journalDays = 30;
	export let fleet: LiveFleet | null = null;
	export let risk: ForvenRiskStatus | null = null;
	export let equity: ForvenEquityHistory | null = null;
	export let selectedSid: string | null = null;
	export let selectedSessionId: string | null = null;
	export let tab: BlotTab = 'fills';
	export let scope: BlotScope = 'all';
	export let busy = false;
	export let now = Date.now();

	const dispatch = createEventDispatcher<{ tab: BlotTab; scope: BlotScope; select: string; close: string }>();

	$: scopedRows = scope === 'strategy' ? rows.filter((row) => row.session.id === selectedSessionId) : rows;
	$: scopedFills = scope === 'strategy' ? fills.filter((fill) => fill.strategy_id === selectedSid) : fills;
	$: scopedJournal = scope === 'strategy' ? journal.filter((event) => event.strategy_id === selectedSid) : journal;
	$: positions = scopedRows.flatMap((row) => row.legs.map((leg, index) => ({ row, leg, math: row.legMath[index] })));
	$: orders = positions.flatMap(({ row, leg, math }) => [
		...(leg.stop !== null ? [{ row, leg, type: 'Stop', trigger: leg.stop, dist: math?.stopDistPct ?? null }] : []),
		...(leg.takeProfit !== null ? [{ row, leg, type: 'Take profit', trigger: leg.takeProfit, dist: math?.targetDistPct ?? null }] : []),
	]);
	$: tabs = [
		['positions', 'Positions', positions.length],
		['orders', mode === 'live' ? 'Open orders' : 'Levels', orders.length],
		['fills', 'Fills', scopedFills.length],
		['decisions', 'Decisions', scopedJournal.length],
		['performance', 'Performance', null],
		['risk', 'Risk', null],
	] as Array<[BlotTab, string, number | null]>;
	$: scopeLabel = scope === 'strategy' ? selectedSid ?? '' : '';

	const th = 'sticky top-0 whitespace-nowrap bg-sc-panel px-2.5 py-1.5 font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3';
	const td = 'whitespace-nowrap border-b border-sc-line px-2.5 py-1.5';
</script>

<section class="overflow-hidden rounded-md border border-sc-line bg-sc-panel" aria-label="Blotter" data-testid="desk-blotter">
	<div class="flex flex-wrap items-center gap-2 border-b border-sc-line px-2 max-md:pb-2">
		<div class="flex min-w-0 flex-1 gap-0.5 overflow-x-auto max-md:basis-full" role="tablist" aria-label="Blotter">
			{#each tabs as [key, text, count]}
				<button
					type="button"
					role="tab"
					class={`whitespace-nowrap border-b-2 px-2.5 pb-2.5 pt-2 text-[13px] ${tab === key ? 'border-sc-ink text-sc-ink' : 'border-transparent text-sc-ink3 hover:text-sc-ink2'}`}
					aria-selected={tab === key}
					on:click={() => dispatch('tab', key)}
				>{text}{#if count !== null}<span class="ml-1 font-plex-mono text-[11px] text-sc-ink4">{count}</span>{/if}</button>
			{/each}
		</div>
		<div class="inline-flex overflow-hidden rounded-full border border-sc-line2" role="group" aria-label="Scope">
			<button type="button" class={`px-2.5 py-0.5 text-[12px] ${scope === 'strategy' ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3'}`} aria-pressed={scope === 'strategy'} on:click={() => dispatch('scope', 'strategy')}>This strategy</button>
			<button type="button" class={`px-2.5 py-0.5 text-[12px] ${scope === 'all' ? 'bg-sc-raise text-sc-ink' : 'text-sc-ink3'}`} aria-pressed={scope === 'all'} on:click={() => dispatch('scope', 'all')}>All {mode}</button>
		</div>
	</div>

	<div class="min-h-[180px]">
		{#if tab === 'positions'}
			{#if positions.length === 0}
				<div class="grid gap-1 px-4 py-7 text-center text-[12.5px] text-sc-ink3"><b class="font-medium text-sc-ink2">No open {mode} positions</b>{scope === 'strategy' ? `for ${selectedSid}. ` : ''}Positions appear here the moment a fill lands{mode === 'live' ? ', with their exchange stop' : ''}.</div>
			{:else}
				<div class="max-h-[460px] overflow-auto">
					<table class="w-full border-collapse text-[12px]" data-testid="desk-positions">
						<thead><tr>
							<th class={`${th} text-left`}>Strategy</th><th class={`${th} text-left`}>Market</th><th class={`${th} text-left`}>Side</th>
							<th class={`${th} text-right`}>Size</th><th class={`${th} text-right`}>Entry</th><th class={`${th} text-right`}>Mark</th><th class={`${th} text-right`}>Value</th>
							<th class={`${th} text-right`}>P&amp;L</th><th class={`${th} text-right`}>R</th><th class={`${th} text-right`}>Stop</th><th class={`${th} text-right`}>Target</th>
							<th class={`${th} text-left`}>{mode === 'live' ? 'Wallet' : 'Book'}</th><th class={`${th} text-right`}>Held</th><th class={th}></th>
						</tr></thead>
						<tbody class="font-plex-mono tabular-nums">
							{#each positions as { row, leg, math } (`${row.session.id}-${leg.id}`)}
								<tr class={`cursor-pointer hover:bg-sc-hover ${row.session.id === selectedSessionId ? 'bg-[#0f1319]' : ''}`} on:click={() => dispatch('select', row.session.id)}>
									<td class={`${td} text-left font-sans`}>{row.sid}</td>
									<td class={`${td} text-left font-sans`}>{row.asset}</td>
									<td class={`${td} text-left`}><span class={`rounded-full border px-2 font-sans text-[11.5px] ${leg.side === 'long' ? 'border-[#139a9f]/55 text-[#5ccac4]' : 'border-[#e0663f]/55 text-[#f2956f]'}`}>{cap1(leg.side)}</span></td>
									<td class={`${td} text-right`}>{fmtQty(leg.size)}</td>
									<td class={`${td} text-right`}>{fmtPx(leg.entry)}</td>
									<td class={`${td} text-right`}>{fmtPx(math?.mark)}</td>
									<td class={`${td} text-right`}>{fmtUsd(math?.notional)}</td>
									<td class={`${td} text-right ${toneClass(math?.pnl)}`}>{fmtUsd(math?.pnl, { signed: true })}</td>
									<td class={`${td} text-right ${toneClass(math?.r)}`}>{math?.r !== null && math?.r !== undefined ? math.r.toFixed(2) : '—'}</td>
									<td class={`${td} text-right`}>{fmtPx(leg.stop)}</td>
									<td class={`${td} text-right`}>{leg.takeProfit !== null ? fmtPx(leg.takeProfit) : '—'}</td>
									<td class={`${td} text-left font-sans`}>{mode === 'live' ? leg.book ?? 'main' : 'paper'}</td>
									<td class={`${td} text-right`}>{math?.heldMs !== null && math?.heldMs !== undefined ? dur(math.heldMs) : '—'}</td>
									<td class={`${td} text-right`}>
										{#if leg.primary}
											<button type="button" class="rounded border border-[#e5574f]/50 px-2 py-0.5 font-sans text-[12px] text-[#f3a39d] hover:bg-[#e5574f]/10 disabled:opacity-45" disabled={busy} on:click|stopPropagation={() => dispatch('close', row.session.id)}>Close</button>
										{/if}
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		{:else if tab === 'orders'}
			{#if orders.length === 0}
				<div class="grid gap-1 px-4 py-7 text-center text-[12.5px] text-sc-ink3">
					<b class="font-medium text-sc-ink2">{mode === 'live' ? 'No resting orders' : 'No levels watched'}</b>
					{mode === 'live' ? 'Stops and targets for open positions rest on Hyperliquid and show here.' : 'Paper stops and targets are watched on the live mark and show here.'}
				</div>
			{:else}
				<div class="max-h-[460px] overflow-auto">
					<table class="w-full border-collapse text-[12px]">
						<thead><tr>
							<th class={`${th} text-left`}>Strategy</th><th class={`${th} text-left`}>Type</th><th class={`${th} text-left`}>Side</th>
							<th class={`${th} text-right`}>Size</th><th class={`${th} text-right`}>Trigger</th><th class={`${th} text-right`}>From mark</th><th class={`${th} text-left`}>How it fills</th>
						</tr></thead>
						<tbody class="font-plex-mono tabular-nums">
							{#each orders as order, index (`${order.row.session.id}-${order.leg.id}-${index}`)}
								<tr class="hover:bg-sc-hover">
									<td class={`${td} text-left font-sans`}>{order.row.sid}</td>
									<td class={`${td} text-left font-sans`}>{order.type}</td>
									<td class={`${td} text-left font-sans`}>{order.leg.side === 'long' ? 'Sell' : 'Buy'}, reduce-only</td>
									<td class={`${td} text-right`}>{fmtQty(order.leg.size)} {order.row.asset}</td>
									<td class={`${td} text-right`}>{fmtPx(order.trigger)}</td>
									<td class={`${td} text-right`}>{fmtPct(order.dist, 2)}</td>
									<td class={`${td} text-left font-sans text-sc-ink2`}>{mode === 'live' ? 'Resting on Hyperliquid' : 'Filled at the level when the live mark touches it'}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			{/if}
		{:else if tab === 'fills'}
			<DeskFillsTable {mode} fills={scopedFills} {selectedSid} />
		{:else if tab === 'decisions'}
			<DeskJournalList {mode} events={scopedJournal} windowDays={journalDays} />
		{:else if tab === 'performance'}
			<DeskPerformance {mode} rows={scopedRows} fills={scopedFills} {equity} {selectedSid} scoped={scope === 'strategy'} {scopeLabel} {now} />
		{:else}
			<DeskRiskPanel {mode} {fleet} {risk} rows={scopedRows} />
		{/if}
	</div>
</section>
