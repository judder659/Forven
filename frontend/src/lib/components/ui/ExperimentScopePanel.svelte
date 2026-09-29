<script lang="ts">
	import { getTimeframeLabel } from '$lib/config/timeframes';
	import { estimateBarCount, formatBarEstimate, formatDateWindowSummary } from '$lib/utils/dateRange';

	export let eyebrow = 'Experiment';
	export let title = '';
	export let description = '';
	export let symbol = '';
	export let timeframe = '1h';
	export let startDate = '';
	export let endDate = '';
	export let accent: 'cyan' | 'blue' | 'violet' | 'amber' | 'orange' | 'teal' | 'rose' = 'cyan';

	// Terminal design language: accents map to the neutral white/#888 scale — the
	// prop stays for callers, but every variant renders the same flat chrome.
	const terminalAccent = {
		eyebrow: 'text-sc-ink3',
		glow: '',
		badge: 'border-sc-line2 bg-sc-panel2 text-sc-ink2',
	};
	const accentStyles = {
		cyan: terminalAccent,
		blue: terminalAccent,
		violet: terminalAccent,
		amber: terminalAccent,
		orange: terminalAccent,
		teal: terminalAccent,
		rose: terminalAccent,
	};

	$: styles = accentStyles[accent];
	$: barEstimateLabel = formatBarEstimate(estimateBarCount(startDate, endDate, timeframe));
	$: timeframeLabel = getTimeframeLabel(timeframe) || '--';
	$: windowSummary = formatDateWindowSummary(startDate, endDate);
</script>

<section class="rounded-md border border-sc-line bg-sc-panel p-4">
	<div class="flex flex-wrap items-start justify-between gap-4">
		<div class="max-w-2xl">
			<div class={`font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] ${styles.eyebrow}`}>{eyebrow}</div>
			<h3 class="mt-2 text-lg font-bold text-sc-ink">{title}</h3>
			{#if description}
				<p class="mt-1 text-sm text-sc-ink2">{description}</p>
			{/if}
		</div>
		<div class="flex flex-wrap items-center gap-2 text-[11px]">
			{#if symbol}
				<span class={`border px-2 py-0.5 font-bold uppercase tracking-wider ${styles.badge}`}>{symbol}</span>
			{/if}
			<span class="rounded-md border border-sc-line2 bg-sc-panel2 px-2 py-0.5 text-sc-ink2">{timeframeLabel}</span>
			<span class="rounded-md border border-sc-line2 bg-sc-panel2 px-2 py-0.5 text-sc-ink3">{windowSummary}</span>
			<span class="rounded-md border border-sc-line2 bg-sc-panel2 px-2 py-0.5 text-sc-ink3">{barEstimateLabel}</span>
		</div>
	</div>
	<div class="mt-4">
		<slot />
	</div>
</section>
