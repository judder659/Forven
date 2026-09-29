<script lang="ts">
	import type { HeldBack } from '$lib/utils/strategyContainer/evidence';
	import { DSR_BAR } from '$lib/utils/strategyContainer/evidence';
	import type { PaperProgress } from '$lib/utils/strategyContainer/lifecycle';
	import { fmtDateUtc, fmtNum, isNum, toNumber } from '$lib/utils/strategyContainer/format';

	export let composite: number | null = null;
	export let floor: number | null = null;
	export let testsPassed: number | null = null;
	export let testsTotal: number | null = null;
	export let testsStale: number | null = null;
	export let heldBack: HeldBack | null = null;
	export let dsr: number | null = null;
	export let dsrTrials: number | null = null;
	export let paper: PaperProgress | null = null;
	export let tradesPerWeek: number | null = null;

	$: heldTrades = toNumber(heldBack?.metrics.total_trades ?? heldBack?.metrics.trades);
	$: heldLabel = heldBack?.verdict === 'PASS' ? 'Pass' : heldBack?.verdict === 'FAIL' ? 'Fail' : heldBack?.state === 'exempt' ? 'Exempt' : heldBack?.state === 'running' ? 'Running' : 'Not run';
	const meter = (value: number | null, cap = 1) => (isNum(value) ? Math.max(0, Math.min(1, value / cap)) * 100 : 0);
</script>

<aside class="grid content-start gap-0 rounded-md border border-sc-line bg-sc-panel px-4 py-3.5" data-testid="standing-card">
	<div class="mb-1 flex items-baseline justify-between gap-2">
		<h2 class="m-0 text-[14px] font-semibold text-sc-ink">Where it stands</h2>
	</div>

	<div class="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-2.5 border-b border-sc-line py-2">
		<span class="text-[12px] text-sc-ink2">Gauntlet composite</span>
		<span class="text-[15px] font-medium font-plex-mono tabular-nums text-sc-ink">{isNum(composite) ? `${composite.toFixed(1)} / 100` : '—'}</span>
		<span class="col-span-2 text-[12px] text-sc-ink3">
			{isNum(floor) ? `Floor ${floor.toFixed(0)}` : 'No floor set'}{isNum(testsPassed) && isNum(testsTotal) ? ` · ${testsPassed} of ${testsTotal} tests passed` : ''}{isNum(testsStale) && testsStale > 0 ? ` · ${testsStale} stale` : ''}
		</span>
		<div class="relative col-span-2 mt-1 h-1.5 rounded-full bg-sc-line2">
			<i class="absolute inset-y-0 left-0 rounded-full bg-sc-ink2" style={`width:${meter(composite, 100)}%`}></i>
			{#if isNum(floor)}<b class="absolute -inset-y-0.5 w-0.5 bg-sc-ink" style={`left:calc(${meter(floor, 100)}% - 1px)`}></b>{/if}
		</div>
	</div>

	<div class="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-2.5 border-b border-sc-line py-2" data-testid="standing-held-back">
		<span class="text-[12px] text-sc-ink2">Held-back test</span>
		<span class={`text-[15px] font-medium ${heldBack?.verdict === 'PASS' ? 'text-[#3cc48f]' : heldBack?.verdict === 'FAIL' ? 'text-[#e5574f]' : 'text-sc-ink2'}`}>{heldLabel}</span>
		{#if heldBack?.start}
			<span class="col-span-2 text-[12px] text-sc-ink3">
				{fmtDateUtc(heldBack.start)} – {fmtDateUtc(heldBack.end)}{isNum(heldTrades) ? ` · ${heldTrades} trades` : ''}{isNum(heldBack.shot) ? ` · shot ${heldBack.shot}${isNum(heldBack.maxShots) ? ` of ${heldBack.maxShots}` : ''}${heldBack.family ? ` for the ${heldBack.family} family` : ''}` : ''}
			</span>
		{/if}
	</div>

	<div class="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-2.5 border-b border-sc-line py-2">
		<span class="text-[12px] text-sc-ink2">Deflated Sharpe</span>
		<span class="text-[15px] font-medium font-plex-mono tabular-nums text-sc-ink">{fmtNum(dsr)}</span>
		<span class="col-span-2 text-[12px] text-sc-ink3">Probability the edge is real{isNum(dsrTrials) ? ` after ${dsrTrials.toLocaleString('en-US')} trials` : ''}; {DSR_BAR} is the usual bar</span>
		<div class="relative col-span-2 mt-1 h-1.5 rounded-full bg-sc-line2">
			<i class="absolute inset-y-0 left-0 rounded-full bg-sc-ink2" style={`width:${meter(dsr)}%`}></i>
			<b class="absolute -inset-y-0.5 w-0.5 bg-sc-ink" style={`left:calc(${DSR_BAR * 100}% - 1px)`}></b>
		</div>
	</div>

	{#if paper}
		<div class="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-2.5 border-b border-sc-line py-2">
			<span class="text-[12px] text-sc-ink2">Paper days</span>
			<span class="text-[15px] font-medium font-plex-mono tabular-nums text-sc-ink">{isNum(paper.days) ? Math.floor(paper.days) : '—'} / {paper.needDays ?? '—'}</span>
			<div class="relative col-span-2 mt-1 h-1.5 rounded-full bg-sc-line2"><i class="absolute inset-y-0 left-0 rounded-full bg-sc-ink2" style={`width:${meter(paper.days, paper.needDays ?? 1)}%`}></i></div>
		</div>
		<div class="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-2.5 py-2">
			<span class="text-[12px] text-sc-ink2">Paper trades</span>
			<span class="text-[15px] font-medium font-plex-mono tabular-nums text-sc-ink">{paper.trades} / {paper.needTrades ?? '—'}</span>
			{#if isNum(tradesPerWeek)}<span class="col-span-2 text-[12px] text-sc-ink3">About {tradesPerWeek.toFixed(1)} a week expected from the backtest</span>{/if}
			<div class="relative col-span-2 mt-1 h-1.5 rounded-full bg-sc-line2"><i class="absolute inset-y-0 left-0 rounded-full bg-sc-ink2" style={`width:${meter(paper.trades, paper.needTrades ?? 1)}%`}></i></div>
		</div>
	{/if}
	<slot />
</aside>
