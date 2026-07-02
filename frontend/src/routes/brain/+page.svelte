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

<div class="brain-page">
	<header class="brain-header">
		<div>
			<h1>Brain</h1>
			<p class="subtitle">{activeMeta.description}</p>
		</div>
	</header>

	<div class="tabs" role="tablist" aria-label="Brain sections">
		{#each TABS as tab (tab.id)}
			<button
				type="button"
				role="tab"
				aria-selected={activeTab === tab.id}
				class:active={activeTab === tab.id}
				on:click={() => setTab(tab.id)}
			>
				{tab.label}
			</button>
		{/each}
	</div>

	<section class="tab-content">
		{#if activeTab === 'overview'}
			<BrainOverviewTab />
		{:else if activeTab === 'decisions'}
			<BrainDecisionsTab />
		{:else if activeTab === 'memory'}
			<BrainMemoryTab />
		{/if}
	</section>
</div>

<style>
	.brain-page {
		padding: 1.5rem 2rem 3rem;
		color: #e5e5e5;
		max-width: 1280px;
		margin: 0 auto;
	}

	.brain-header {
		display: flex;
		justify-content: space-between;
		align-items: flex-end;
		margin-bottom: 1rem;
	}

	.brain-header h1 {
		font-size: 1.125rem;
		font-weight: 700;
		margin: 0;
		text-transform: uppercase;
		letter-spacing: 0.15em;
	}

	.subtitle {
		color: #888;
		margin: 0.25rem 0 0;
		font-size: 0.875rem;
	}

	.tabs {
		display: flex;
		gap: 0;
		border-bottom: 1px solid #222;
		margin-bottom: 1.5rem;
	}

	.tabs button {
		background: transparent;
		border: none;
		color: #888;
		padding: 0.625rem 1.25rem;
		font-size: 0.875rem;
		cursor: pointer;
		border-bottom: 2px solid transparent;
		transition: color 120ms ease, border-color 120ms ease;
	}

	.tabs button:hover {
		color: #ddd;
	}

	.tabs button.active {
		color: #fff;
		border-bottom-color: #fff;
	}

	.tab-content {
		min-height: 400px;
	}
</style>
