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
		eyebrow: 'text-[#666]',
		glow: '',
		badge: 'border-[#333] bg-[#111] text-[#888]',
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

<section class="border border-[#222] bg-[#050505] p-4">
	<div class="flex flex-wrap items-start justify-between gap-4">
		<div class="max-w-2xl">
			<div class={`text-[10px] uppercase tracking-widest ${styles.eyebrow}`}>{eyebrow}</div>
			<h3 class="mt-2 text-lg font-bold text-white">{title}</h3>
			{#if description}
				<p class="mt-1 text-sm text-[#888]">{description}</p>
			{/if}
		</div>
		<div class="flex flex-wrap items-center gap-2 text-[11px]">
			{#if symbol}
				<span class={`border px-2 py-0.5 font-bold uppercase tracking-wider ${styles.badge}`}>{symbol}</span>
			{/if}
			<span class="border border-[#333] bg-[#111] px-2 py-0.5 text-[#888]">{timeframeLabel}</span>
			<span class="border border-[#333] bg-[#111] px-2 py-0.5 text-[#666]">{windowSummary}</span>
			<span class="border border-[#333] bg-[#111] px-2 py-0.5 text-[#666]">{barEstimateLabel}</span>
		</div>
	</div>
	<div class="mt-4">
		<slot />
	</div>
</section>
