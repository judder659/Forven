<script lang="ts" context="module">
	export type PositionActionKind = 'close' | 'partial' | 'breakeven' | 'flip' | 'pause' | 'resume' | 'setStop' | 'clearStop' | 'setTarget' | 'clearTarget';
	export interface PositionAction {
		kind: PositionActionKind;
		pct?: number;
		price?: number;
	}
</script>

<script lang="ts">
	/** One open leg: P&L in dollars and R, the price ladder, protection, funding and controls. */
	import { createEventDispatcher } from 'svelte';
	import type { AssetMarketContext, DeskMode } from '$lib/api/desk';
	import { cap1, dur, fmtPct, fmtPx, fmtQty, fmtUsd, num, toneClass, MINUS } from '$lib/utils/tradingDesk/format';
	import { fundingFlowText, positionFundingPerHour } from '$lib/utils/tradingDesk/market';
	import { ladder, type Leg, type LegMath } from '$lib/utils/tradingDesk/position';

	export let mode: DeskMode;
	export let leg: Leg;
	export let math: LegMath;
	export let asset: string;
	export let timeframe: string;
	export let leverage = 1;
	export let market: AssetMarketContext | null = null;
	export let busy = false;
	export let legIndex = 0;
	export let legCount = 1;

	const dispatch = createEventDispatcher<{ action: PositionAction }>();

	let stopInput: string | number | null = '';
	let targetInput: string | number | null = '';
	let lastLegId = '';
	$: if (leg.id !== lastLegId) {
		lastLegId = leg.id;
		stopInput = leg.stop !== null ? String(leg.stop) : '';
		targetInput = leg.takeProfit !== null ? String(leg.takeProfit) : '';
	}

	$: geometry = ladder(leg, math.mark);
	$: fundingPerHour = positionFundingPerHour(leg.side, leg.size, math.mark, market?.funding?.rate_hourly);
	$: controllable = leg.primary;
	$: stopValue = num(stopInput);
	$: targetValue = num(targetInput);
	$: stopUnchanged = stopValue !== null && leg.stop !== null && Math.abs(stopValue - leg.stop) < 1e-9;
	$: targetUnchanged = targetValue !== null && leg.takeProfit !== null && Math.abs(targetValue - leg.takeProfit) < 1e-9;

	const act = 'rounded border border-sc-line2 bg-sc-panel px-1 py-1.5 text-[12px] text-sc-ink hover:border-sc-ink4 disabled:cursor-not-allowed disabled:opacity-45';
	const input = 'w-full min-w-0 rounded border border-sc-line2 bg-sc-bg px-2 py-1 font-plex-mono text-[12px] text-sc-ink focus:border-sc-ink4 focus:outline-none';
	const btn = 'whitespace-nowrap rounded-md border border-sc-line2 bg-sc-panel2 px-2.5 py-1 text-[12px] font-medium text-sc-ink hover:border-sc-ink4 disabled:cursor-not-allowed disabled:opacity-45';

	function labelStyle(anchor: 'start' | 'middle' | 'end', at: number): string {
		const shift = anchor === 'end' ? '-100%' : anchor === 'start' ? '0' : '-50%';
		return `left:${Math.max(0, Math.min(100, at))}%;transform:translateX(${shift})`;
	}
</script>

<article class="grid gap-2.5 rounded-md border border-sc-line2 bg-sc-panel2 p-3" data-testid="desk-position-card">
	<div class="flex flex-wrap items-center gap-x-2.5 gap-y-1">
		<span class={`rounded-full border px-2 text-[11.5px] ${leg.side === 'long' ? 'border-[#139a9f]/55 text-[#5ccac4]' : 'border-[#e0663f]/55 text-[#f2956f]'}`}>{cap1(leg.side)}</span>
		<b class="font-plex-mono font-medium text-sc-ink">{fmtQty(leg.size)} {asset}</b>
		<span class="text-[12px] text-sc-ink3">{leverage}× · {mode === 'live' ? (leg.book ? `${leg.book} wallet` : 'main wallet') : 'paper book'}</span>
		{#if legCount > 1}<span class="rounded-full border border-sc-line2 px-2 text-[11px] text-sc-ink2">Leg {legIndex + 1} of {legCount}</span>{/if}
		{#if leg.manualPause}<span class="rounded-full border border-[#e7b24a]/45 px-2 text-[11px] text-[#e7b24a]">Auto-management paused</span>{/if}
	</div>
	<div class={`font-plex-mono text-[24px] font-medium ${toneClass(math.pnl)}`}>
		{fmtUsd(math.pnl, { signed: true })}
		<small class="ml-1.5 font-sans text-[12px] font-normal text-sc-ink3">{fmtPct(math.pnlPctOfMargin)} of margin{#if math.r !== null} · {math.r >= 0 ? '+' : MINUS}{Math.abs(math.r).toFixed(2)}R{/if}</small>
	</div>

	<div class="relative mx-1.5 mt-1 h-14" aria-label="Stop, entry, mark and target on one scale">
		<div class="absolute inset-x-0 top-6 h-1.5 rounded bg-sc-line2"></div>
		{#if geometry.lossZone}<i class="absolute top-6 h-1.5 bg-[#e0663f]/45" style={`left:${geometry.lossZone.from}%;width:${geometry.lossZone.to - geometry.lossZone.from}%`}></i>{/if}
		<i class="absolute top-6 h-1.5 bg-[#139a9f]/45" style={`left:${geometry.gainZone.from}%;width:${geometry.gainZone.to - geometry.gainZone.from}%`}></i>
		{#each geometry.marks as mark (mark.key)}
			{#if mark.key === 'mark'}
				<i class="absolute top-[21px] -ml-1.5 h-3 w-3 rounded-full border-2 border-sc-panel2 bg-sc-ink" style={`left:${mark.at}%`} title={`Mark ${fmtPx(mark.price)}`}></i>
			{:else}
				<i class={`absolute top-[18px] -ml-px h-[18px] w-0.5 ${mark.key === 'stop' ? 'bg-[#e0663f]' : mark.key === 'target' ? 'bg-[#139a9f]' : 'bg-sc-ink2'}`} style={`left:${mark.at}%`}></i>
			{/if}
			<span class={`absolute whitespace-nowrap text-[11px] text-sc-ink3 ${mark.row === 'top' ? 'top-0' : 'top-10'}`} style={labelStyle(mark.anchor, mark.at)}>
				{mark.key === 'stop' ? 'Stop' : mark.key === 'target' ? 'Target' : mark.key === 'entry' ? 'Entry' : 'Mark'}
				<b class="font-plex-mono font-medium text-sc-ink2">{fmtPx(mark.price)}</b>
				{#if mark.key === 'stop' && math.stopDistPct !== null}<span class="text-[#f2956f]"> {fmtPct(math.stopDistPct)}</span>{/if}
			</span>
		{/each}
	</div>

	<dl class="grid grid-cols-2 gap-x-3.5 gap-y-1.5">
		<div><dt class="text-[11.5px] text-sc-ink3">Entry</dt><dd class="font-plex-mono text-[12.5px] text-sc-ink">{fmtPx(leg.entry)}</dd></div>
		<div><dt class="text-[11.5px] text-sc-ink3">Mark</dt><dd class="font-plex-mono text-[12.5px] text-sc-ink">{fmtPx(math.mark)}</dd></div>
		<div><dt class="text-[11.5px] text-sc-ink3">Position value</dt><dd class="font-plex-mono text-[12.5px] text-sc-ink">{fmtUsd(math.notional)}</dd></div>
		<div><dt class="text-[11.5px] text-sc-ink3">Margin</dt><dd class="font-plex-mono text-[12.5px] text-sc-ink">{fmtUsd(math.margin)}</dd></div>
		<div><dt class="text-[11.5px] text-sc-ink3">Loss if the stop fills</dt><dd class="font-plex-mono text-[12.5px] text-[#f2956f]">{math.risk !== null ? fmtUsd(-math.risk) : 'No stop'}</dd></div>
		<div><dt class="text-[11.5px] text-sc-ink3">Target</dt><dd class="font-plex-mono text-[12.5px] text-sc-ink">{leg.takeProfit !== null ? fmtPx(leg.takeProfit) : 'Strategy exit'}</dd></div>
		<div><dt class="text-[11.5px] text-sc-ink3">Time in trade</dt><dd class="font-plex-mono text-[12.5px] text-sc-ink">{math.heldMs !== null ? dur(math.heldMs) : '—'}</dd></div>
		<div><dt class="text-[11.5px] text-sc-ink3">Bars held</dt><dd class="font-plex-mono text-[12.5px] text-sc-ink">{math.barsHeld ?? '—'} × {timeframe}</dd></div>
	</dl>

	<div class="grid grid-cols-[16px_minmax(0,1fr)] gap-1.5 border-t border-sc-line pt-2 text-[12px] text-sc-ink2">
		<span class={`font-plex-mono font-semibold ${leg.stop !== null ? 'text-[#3cc48f]' : 'text-[#e7b24a]'}`}>{leg.stop !== null ? '✓' : '!'}</span>
		<span>
			{#if leg.stop === null}
				No stop on this position.
			{:else if mode === 'live'}
				Stop at <b class="font-plex-mono font-medium text-sc-ink">{fmtPx(leg.stop)}</b>{leg.stopSource ? ` (${leg.stopSource.replace(/_/g, ' ')})` : ''}, resting on Hyperliquid as a reduce-only order.
			{:else}
				Stop at <b class="font-plex-mono font-medium text-sc-ink">{fmtPx(leg.stop)}</b>; the daemon fills it at that level when the live mark touches it.
			{/if}
			{leg.manualPause ? 'You own the exit.' : 'The strategy manages the exit.'}
			<span class="block text-sc-ink3">{fundingFlowText(fundingPerHour)}{market?.funding ? ` at ${fmtPct(market.funding.rate_hourly * 100, 4, false)}/h` : ''}.</span>
		</span>
	</div>

	{#if controllable}
		<div class="grid grid-cols-3 gap-1.5">
			<button type="button" class={`${act} border-[#e5574f]/50 text-[#f3a39d]`} disabled={busy} on:click={() => dispatch('action', { kind: 'close' })}>Close</button>
			<button type="button" class={act} disabled={busy} on:click={() => dispatch('action', { kind: 'partial', pct: 50 })}>Close 50%</button>
			<button type="button" class={act} disabled={busy} on:click={() => dispatch('action', { kind: 'partial', pct: 25 })}>Close 25%</button>
			<button
				type="button"
				class={act}
				disabled={busy || !math.inProfit}
				title={math.inProfit ? 'Move the stop to the entry price' : `Price must be past entry first (${leg.side === 'short' ? 'below' : 'above'} ${fmtPx(leg.entry)})`}
				on:click={() => dispatch('action', { kind: 'breakeven' })}
			>Stop to entry</button>
			<button type="button" class={act} disabled={busy} on:click={() => dispatch('action', { kind: 'flip' })}>Flip</button>
			<button type="button" class={act} disabled={busy} on:click={() => dispatch('action', { kind: leg.manualPause ? 'resume' : 'pause' })}>{leg.manualPause ? 'Resume auto' : 'Pause auto'}</button>
		</div>
		<div class="grid grid-cols-[44px_minmax(0,1fr)_auto_auto] items-center gap-1.5">
			<label class="text-[12px] text-sc-ink3" for={`stop-${leg.id}`}>Stop</label>
			<input id={`stop-${leg.id}`} class={input} type="number" step="any" bind:value={stopInput} disabled={busy} />
			<button type="button" class={btn} disabled={busy || stopValue === null || stopValue <= 0 || stopUnchanged} on:click={() => stopValue !== null && dispatch('action', { kind: 'setStop', price: stopValue })}>Move</button>
			<button type="button" class={btn} disabled={busy || leg.stop === null || mode === 'live'} title={mode === 'live' ? 'A live position keeps a stop; move it instead' : 'Remove the stop'} on:click={() => dispatch('action', { kind: 'clearStop' })}>Clear</button>
		</div>
		<div class="grid grid-cols-[44px_minmax(0,1fr)_auto_auto] items-center gap-1.5">
			<label class="text-[12px] text-sc-ink3" for={`target-${leg.id}`}>Target</label>
			<input id={`target-${leg.id}`} class={input} type="number" step="any" placeholder="none" bind:value={targetInput} disabled={busy} />
			<button type="button" class={btn} disabled={busy || targetValue === null || targetValue <= 0 || targetUnchanged} on:click={() => targetValue !== null && dispatch('action', { kind: 'setTarget', price: targetValue })}>Set</button>
			<button type="button" class={btn} disabled={busy || leg.takeProfit === null} on:click={() => dispatch('action', { kind: 'clearTarget' })}>Clear</button>
		</div>
	{:else}
		<p class="text-[12px] text-sc-ink3">Manual controls act on the first leg. This {leg.side} leg closes by the strategy{mode === 'live' ? ' or on the exchange' : ''}.</p>
	{/if}
</article>
