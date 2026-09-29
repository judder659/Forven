<script lang="ts">
	import { DSR_BAR } from '$lib/utils/strategyContainer/evidence';
	import { fmtNum, isNum } from '$lib/utils/strategyContainer/format';

	/** Probability the Sharpe beats luck once every variant tried is counted (advisory). */
	export let dsr: number | null = null;
	export let trials: number | null = null;

	$: tone = isNum(dsr) ? (dsr >= DSR_BAR ? 'bg-[#3cc48f]' : 'bg-[#e7b24a]') : 'bg-[#444]';
</script>

<div class="grid gap-3" data-testid="deflated-sharpe-card">
	<div class="grid grid-cols-[minmax(0,1fr)_auto] items-baseline gap-x-2.5">
		<span class="text-[12px] text-[#aab1bc]">Deflated Sharpe</span>
		<span class="text-[20px] font-medium tabular-nums text-white">{fmtNum(dsr)}</span>
		<div class="relative col-span-2 mt-1.5 h-2 bg-[#2a2f38]">
			<i class={`absolute inset-y-0 left-0 ${tone}`} style={`width:${isNum(dsr) ? Math.max(0, Math.min(1, dsr)) * 100 : 0}%`}></i>
			<b class="absolute -inset-y-1 w-0.5 bg-white" style={`left:calc(${DSR_BAR * 100}% - 1px)`}></b>
		</div>
		<span class="col-span-2 mt-1 text-[10.5px] text-[#666]">0 · 0.5 coin-flip · {DSR_BAR} conventional bar · 1</span>
	</div>
	<p class="m-0 text-[11px] leading-relaxed text-[#777]">
		{#if isNum(dsr)}
			About {Math.round(dsr * 100)}% of the evidence says the Sharpe beats luck once {isNum(trials) ? `the ${trials.toLocaleString('en-US')} variants` : 'every variant'} the pipeline tried for this family {isNum(trials) ? 'are' : 'is'} counted.
		{:else}
			Not computed for this strategy yet.
		{/if}
		It is advisory: the gates judge walk-forward, Monte Carlo, jitter, costs, regimes and the held-back test.
	</p>
</div>
