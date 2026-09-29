<script lang="ts">
	import { fmtEtaRates, fmtEtaWindow, type GateEta } from '$lib/utils/strategyContainer/lifecycle';

	/** The next gate: its live checklist (slotted) and, on paper, when it can pass. */
	export let title = 'Next gate';
	export let eta: GateEta | null = null;
	export let needDays: number | null = null;
	export let needTrades: number | null = null;
</script>

<aside class="grid content-start gap-3 rounded-md border border-sc-line bg-sc-panel px-4 py-3.5" id="gate-card" data-testid="gate-card">
	<div class="flex items-baseline justify-between gap-2">
		<h2 class="m-0 text-[14px] font-semibold text-sc-ink">{title}</h2>
		<span class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">configured thresholds</span>
	</div>
	<slot />
	{#if eta}
		<p class="m-0 text-[12px] leading-relaxed text-sc-ink3" data-testid="gate-eta">
			Needs {needDays ?? '—'} days and {needTrades ?? '—'} closed paper trades ({eta.remainingTrades} to go). At the backtest's {fmtEtaRates(eta)} trades a month the earliest pass is
			<b class="font-medium text-sc-ink">{fmtEtaWindow(eta)}</b>.
		</p>
	{/if}
</aside>
