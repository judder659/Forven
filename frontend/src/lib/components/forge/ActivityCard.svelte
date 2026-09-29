<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { ActivityItem } from '$lib/utils/forge/activity';
	import { TONE_TEXT } from '$lib/utils/forge/status';
	import { ago, shortDateTime } from '$lib/utils/forge/time';

	export let items: ActivityItem[] = [];
	export let loading = false;
	export let error: string | null = null;
	/** Re-render clock for the "x ago" labels. */
	export let now = Date.now();

	const dispatch = createEventDispatcher<{ peek: string }>();

	const GLYPH: Record<ActivityItem['kind'], string> = {
		promoted: '↑',
		archived: '↓',
		demoted: '↘',
		revived: '↺',
		approval: '!',
	};
</script>

<article class="flex min-h-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel" data-testid="forge-activity">
	<header class="flex items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
		<h2 class="text-[13px] font-semibold text-sc-ink">What just happened</h2>
		<span class="text-[11px] text-sc-ink3">Pipeline moves, newest first</span>
	</header>
	<div class="min-h-0 flex-1 overflow-y-auto">
		{#if error && items.length === 0}
			<p class="m-0 px-3.5 py-4 text-[12px] text-[#f2956f]">{error}</p>
		{:else if loading && items.length === 0}
			<div class="grid gap-2 px-3.5 py-3" aria-busy="true">
				{#each [0, 1, 2, 3] as i (i)}
					<div class="h-7 animate-pulse rounded bg-sc-raise/60"></div>
				{/each}
			</div>
		{:else if items.length === 0}
			<p class="m-0 px-3.5 py-6 text-center text-[12px] text-sc-ink3">No pipeline moves recorded yet.</p>
		{:else}
			<ol class="m-0 list-none p-0">
				{#each items as item (item.key)}
					<li class="grid grid-cols-[18px_minmax(0,1fr)_auto] items-start gap-2 px-3.5 py-1.5 hover:bg-sc-hover">
						<span class={`mt-px text-center font-plex-mono text-[12px] leading-5 ${TONE_TEXT[item.tone]}`} aria-hidden="true">{GLYPH[item.kind]}</span>
						<div class="min-w-0">
							{#if item.count === 1 && item.strategyIds[0]}
								<button
									type="button"
									class="block max-w-full truncate text-left text-[12px] leading-5 text-sc-ink hover:underline"
									title="Show details"
									on:click={() => dispatch('peek', item.strategyIds[0])}
								>{item.title}</button>
							{:else}
								<span class="block truncate text-[12px] leading-5 text-sc-ink" title={item.strategyIds.join(', ')}>{item.title}</span>
							{/if}
							{#if item.detail || item.actor}
								<span class="block truncate text-[11px] text-sc-ink3" title={item.detail}>
									{#if item.count === 1 && item.strategyIds[0] && !item.title.startsWith(item.strategyIds[0])}<span class="font-plex-mono">{item.strategyIds[0]}</span> · {/if}{item.detail}{#if item.actor}{item.detail ? ' · ' : ''}by {item.actor}{/if}
								</span>
							{/if}
						</div>
						<time class="whitespace-nowrap pt-0.5 text-[11px] text-sc-ink3" datetime={item.at} title={shortDateTime(item.at)}>{ago(item.at, now)}</time>
					</li>
				{/each}
			</ol>
		{/if}
	</div>
</article>
