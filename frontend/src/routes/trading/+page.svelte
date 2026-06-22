<script lang="ts">
	import PaperTrades from '$lib/components/trading/PaperTrades.svelte';
	import PaperSessionSummary from '$lib/components/dashboard/PaperSessionSummary.svelte';
	import type { ForvenDashboardResponse } from '$lib/api';

	export let data: { dashboard: ForvenDashboardResponse | null };

	// Session scope. 'paper' is the default (fast); 'live' / 'all' pull in deployed
	// strategies via include_deployed so the manual controls can drive REAL positions.
	type SessionView = 'paper' | 'live' | 'all';
	let view: SessionView = 'paper';
	const VIEWS: { id: SessionView; label: string; hint: string }[] = [
		{ id: 'paper', label: 'Paper', hint: 'Paper-stage sessions only (fast).' },
		{ id: 'live', label: 'Live', hint: 'Deployed / graduated strategies — REAL orders.' },
		{ id: 'all', label: 'All', hint: 'Paper + live sessions together.' },
	];
</script>

<svelte:head>
	<title>Trades | Forven</title>
	<meta name="description" content="Manage paper and live positions with manual controls, chart overlays, signals, and execution history." />
</svelte:head>

<div class="workspace-layout flex-col">
	<div class="flex-shrink-0 px-2 pt-2">
		<div class="flex items-center gap-1 mb-2" data-testid="session-view-toggle">
			<span class="text-[10px] uppercase tracking-wider text-gray-500 mr-1">Sessions</span>
			{#each VIEWS as v (v.id)}
				<button
					class="terminal-button text-[10px] py-0 px-2 {view === v.id ? 'bg-[#111] text-white border-white' : ''} {v.id === 'live' ? 'text-red-400' : ''}"
					title={v.hint}
					on:click={() => (view = v.id)}
				>{v.label}</button>
			{/each}
			{#if view !== 'paper'}
				<span class="text-[10px] text-gray-500 ml-2">Loading deployed sessions can take a few seconds.</span>
			{/if}
		</div>
		<PaperSessionSummary />
	</div>
	<div class="flex-1 flex flex-col overflow-hidden">
		{#key view}
			<PaperTrades dashboard={data.dashboard} {view} />
		{/key}
	</div>
</div>
