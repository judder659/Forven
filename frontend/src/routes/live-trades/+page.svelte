<script lang="ts">
	import PaperTrades from '$lib/components/trading/PaperTrades.svelte';
	import type { ForvenDashboardResponse } from '$lib/api';

	export let data: { dashboard: ForvenDashboardResponse | null };
</script>

<svelte:head>
	<title>Live Trades | Forven</title>
	<meta name="description" content="Manage live positions of deployed strategies with manual controls — actions here drive REAL exchange orders." />
</svelte:head>

<div class="workspace-layout flex-col">
	<div class="flex-shrink-0 px-2 pt-2">
		<div class="flex items-center gap-1 mb-2">
			<span class="text-[10px] uppercase tracking-wider text-red-400">Live strategies — REAL exchange orders</span>
			<span class="text-[10px] text-[#666] ml-2">Loading deployed sessions can take a few seconds.</span>
			<a
				href="/all-trades"
				data-sveltekit-preload-data="hover"
				class="terminal-button text-[10px] py-0 px-2 ml-auto"
				title="Full trade ledger — every paper & live trade, filterable & sortable"
			>All Trades →</a>
		</div>
	</div>
	<div class="flex-1 flex flex-col overflow-hidden">
		<PaperTrades dashboard={data.dashboard} view="live" />
	</div>
</div>
