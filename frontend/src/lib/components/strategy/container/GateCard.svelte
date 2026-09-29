<script lang="ts">
	import type { GateEta } from '$lib/utils/strategyContainer/lifecycle';
	import { fmtDateUtc, fmtNum } from '$lib/utils/strategyContainer/format';

	/** The next gate: its live checklist (slotted) and, on paper, when it can pass. */
	export let title = 'Next gate';
	export let eta: GateEta | null = null;
	export let needDays: number | null = null;
	export let needTrades: number | null = null;
</script>

<aside class="grid content-start gap-3 border border-[#1d1d1d] bg-[#090909] p-4" id="gate-card" data-testid="gate-card">
	<div class="flex items-baseline justify-between gap-2">
		<h2 class="m-0 text-[13px] font-semibold text-white">{title}</h2>
		<span class="text-[10px] uppercase tracking-[0.2em] text-[#555]">configured thresholds</span>
	</div>
	<slot />
	{#if eta}
		<p class="m-0 text-[11px] leading-relaxed text-[#777]" data-testid="gate-eta">
			Needs {needDays ?? '—'} days and {needTrades ?? '—'} closed paper trades ({eta.remainingTrades} to go). At the backtest's {fmtNum(eta.rates[0], 1)}–{fmtNum(eta.rates[1], 1)} trades a month the earliest pass is
			<b class="font-medium text-white">{fmtDateUtc(eta.earliest)} – {fmtDateUtc(eta.latest)}</b>.
		</p>
	{/if}
</aside>
