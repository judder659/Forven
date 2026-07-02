<script lang="ts">
	import { onMount, onDestroy } from 'svelte';
	import { page } from '$app/stores';
	import PaperTrades from '$lib/components/trading/PaperTrades.svelte';
	import PaperSessionSummary from '$lib/components/dashboard/PaperSessionSummary.svelte';
	import type { ForvenDashboardResponse } from '$lib/api';

	export let data: { dashboard: ForvenDashboardResponse | null };

	// Session scope. 'paper' is the default (fast); 'live' / 'all' pull in deployed
	// strategies via include_deployed so the manual controls can drive REAL positions.
	type SessionView = 'paper' | 'live' | 'all';
	// Honor an inbound ?view= deep-link (e.g. from the All Trades blotter) so a live
	// strategy's session is loaded on first mount and PaperTrades can select it.
	function initialView(): SessionView {
		const v = $page.url.searchParams.get('view');
		return v === 'live' || v === 'all' ? v : 'paper';
	}
	let view: SessionView = initialView();

	// The position alert (paper-only) can fire while we're on the 'live' view, where
	// paper sessions aren't loaded. Drop back to 'paper' so the remounted PaperTrades
	// reads the stored session id on mount and selects it. 'paper'/'all' already list
	// the session, so PaperTrades handles those in place without a view change.
	function handleSelectSessionRequest() {
		if (view === 'live') view = 'paper';
	}

	onMount(() => {
		window.addEventListener('forven:select-session', handleSelectSessionRequest);
	});

	onDestroy(() => {
		if (typeof window !== 'undefined') {
			window.removeEventListener('forven:select-session', handleSelectSessionRequest);
		}
	});
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
			<span class="text-[10px] uppercase tracking-wider text-[#666] mr-1">Sessions</span>
			{#each VIEWS as v (v.id)}
				<button
					class="terminal-button text-[10px] py-0 px-2 {view === v.id ? 'bg-[#111] text-white border-white' : ''} {v.id === 'live' ? 'text-red-400' : ''}"
					title={v.hint}
					on:click={() => (view = v.id)}
				>{v.label}</button>
			{/each}
			{#if view !== 'paper'}
				<span class="text-[10px] text-[#666] ml-2">Loading deployed sessions can take a few seconds.</span>
			{/if}
			<a
				href="/all-trades"
				data-sveltekit-preload-data="hover"
				class="terminal-button text-[10px] py-0 px-2 ml-auto"
				title="Full trade ledger — every paper & live trade, filterable & sortable"
			>All Trades →</a>
		</div>
		<PaperSessionSummary />
	</div>
	<div class="flex-1 flex flex-col overflow-hidden">
		{#key view}
			<PaperTrades dashboard={data.dashboard} {view} />
		{/key}
	</div>
</div>
