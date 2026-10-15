<script lang="ts">
	import { onMount } from 'svelte';
	import { goto } from '$app/navigation';
	import { page } from '$app/stores';
	import BrainOverviewTab from '$lib/components/brain/BrainOverviewTab.svelte';
	import BrainMemoryTab from '$lib/components/brain/BrainMemoryTab.svelte';
	import BrainDecisionsTab from '$lib/components/brain/BrainDecisionsTab.svelte';

	// Decisions re-linked (2026-07-02): the live brain worker now records a
	// brain_decisions row per autonomous cycle (agent-overhaul), so the ledger
	// populates again. brain_lessons was removed entirely (never gained a writer).
	type Tab = 'overview' | 'decisions' | 'memory';

	const TABS: { id: Tab; label: string; description: string }[] = [
		{
			id: 'overview',
			label: 'Overview',
			description: 'Autonomy state, actions, blockers, and memory.'
		},
		{
			id: 'decisions',
			label: 'Decisions',
			description: 'What the Brain saw, what it decided, the tasks it spawned, and how each decision turned out.'
		},
		{
			id: 'memory',
			label: 'Working Notes',
			description: 'Short-term operational notes the Brain carries between cycles.'
		}
	];

	let activeTab: Tab = 'overview';

	function tabFromUrl(searchParams: URLSearchParams): Tab {
		const raw = searchParams.get('tab');
		if (raw === 'overview' || raw === 'decisions' || raw === 'memory') return raw;
		return 'overview';
	}

	function setTab(tab: Tab) {
		activeTab = tab;
		const url = new URL($page.url);
		url.searchParams.set('tab', tab);
		goto(url.pathname + url.search, { replaceState: true, keepFocus: true, noScroll: true });
	}

	onMount(() => {
		activeTab = tabFromUrl($page.url.searchParams);
	});

	$: activeTab = tabFromUrl($page.url.searchParams);
	$: activeMeta = TABS.find((t) => t.id === activeTab) ?? TABS[0];
</script>

<svelte:head>
	<title>Brain — Forven</title>
</svelte:head>

<div class="min-h-full bg-sc-bg" data-testid="brain-page">
	<header class="border-b border-sc-line bg-sc-panel">
		<div class="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 px-5 pb-3 pt-4">
			<div class="min-w-0 max-w-[980px] flex-1">
				<h1 class="text-[22px] font-semibold tracking-[-0.01em] text-sc-ink">Brain</h1>
				<p class="m-0 mt-1.5 text-[13px] leading-relaxed text-sc-ink2">{activeMeta.description}</p>
			</div>
		</div>
		<div class="flex flex-wrap gap-1 px-5" role="tablist" aria-label="Brain sections">
			{#each TABS as tab (tab.id)}
				<button
					type="button"
					role="tab"
					aria-selected={activeTab === tab.id}
					class={`-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-[13px] transition-colors ${activeTab === tab.id ? 'border-sc-ink text-sc-ink' : 'border-transparent text-sc-ink3 hover:text-sc-ink'}`}
					on:click={() => setTab(tab.id)}
				>
					{tab.label}
				</button>
			{/each}
		</div>
	</header>

	<section class="min-h-[400px] px-5 pb-10 pt-4">
		{#if activeTab === 'overview'}
			<BrainOverviewTab />
		{:else if activeTab === 'decisions'}
			<BrainDecisionsTab />
		{:else if activeTab === 'memory'}
			<BrainMemoryTab />
		{/if}
	</section>
</div>
