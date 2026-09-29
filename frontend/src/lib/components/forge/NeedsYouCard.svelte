<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { AttentionItem } from '$lib/utils/forge/attention';
	import { TONE_DOT } from '$lib/utils/forge/status';

	export let items: AttentionItem[] = [];
	/** True until the gate explainer has answered once. */
	export let loading = false;
	export let error: string | null = null;

	const dispatch = createEventDispatcher<{ peek: string }>();
</script>

<article class="flex min-h-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel" data-testid="forge-needs-you">
	<header class="flex items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
		<h2 class="flex items-center gap-2 text-[13px] font-semibold text-sc-ink">
			Needs you
			{#if !loading && items.length > 0}
				<span class="rounded-full bg-[#e7b24a]/15 px-1.5 font-plex-mono text-[11px] font-medium text-[#e7b24a]">{items.length}</span>
			{/if}
		</h2>
		<span class="text-[11px] text-sc-ink3">Only what can't move without you</span>
	</header>
	<div class="min-h-0 flex-1 overflow-y-auto">
		{#if error && items.length === 0}
			<p class="m-0 px-3.5 py-4 text-[12px] text-[#f2956f]">{error}</p>
		{:else if loading && items.length === 0}
			<div class="grid gap-2 px-3.5 py-3" aria-busy="true">
				{#each [0, 1, 2] as i (i)}
					<div class="h-9 animate-pulse rounded bg-sc-raise/60"></div>
				{/each}
				<p class="m-0 text-[11px] text-sc-ink3">Checking every gate…</p>
			</div>
		{:else if items.length === 0}
			<div class="grid place-items-center gap-1 px-3.5 py-7 text-center">
				<span class="grid h-7 w-7 place-items-center rounded-full bg-[#3cc48f]/15 text-[13px] text-[#3cc48f]" aria-hidden="true">✓</span>
				<p class="m-0 text-[12px] text-sc-ink2">Nothing needs you.</p>
				<p class="m-0 text-[11px] text-sc-ink3">Evidence accumulates and sweeps run on their own.</p>
			</div>
		{:else}
			<ul class="m-0 list-none divide-y divide-sc-line p-0">
				{#each items as item (item.key)}
					<li class="group flex items-start gap-2.5 px-3.5 py-2 hover:bg-sc-hover">
						<span class={`mt-[5px] h-2 w-2 shrink-0 rounded-full ${TONE_DOT[item.tone]}`} aria-hidden="true"></span>
						<div class="min-w-0 flex-1">
							{#if item.strategyId}
								<button
									type="button"
									class="block max-w-full truncate text-left text-[12px] font-medium text-sc-ink hover:underline"
									title="Show details"
									on:click={() => item.strategyId && dispatch('peek', item.strategyId)}
								>{item.title}</button>
							{:else}
								<span class="block truncate text-[12px] font-medium text-sc-ink">{item.title}</span>
							{/if}
							<span class="block truncate text-[11px] text-sc-ink2" title={`${item.meta} — ${item.detail}`}>
								<span class="font-plex-mono text-[10.5px] text-sc-ink3">{item.meta}</span><span class="text-sc-ink4">{' · '}</span>{item.detail}
							</span>
						</div>
						<a
							href={item.href}
							class="shrink-0 rounded border border-sc-line2 px-2 py-0.5 text-[11px] text-sc-ink2 transition-colors hover:border-sc-ink hover:text-sc-ink"
						>{item.actionLabel}</a>
					</li>
				{/each}
			</ul>
			{#if loading}
				<p class="m-0 flex items-center gap-2 border-t border-sc-line px-3.5 py-2 text-[11px] text-sc-ink3" aria-busy="true">
					<span class="h-1.5 w-1.5 animate-pulse rounded-full bg-sc-ink3" aria-hidden="true"></span>
					Still checking every gate — approvals and blocked strategies will join this list.
				</p>
			{/if}
		{/if}
	</div>
</article>
