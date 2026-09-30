<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { AttentionAction, AttentionItem } from '$lib/utils/agentsHub/attention';
	import { TONE_DOT } from '$lib/utils/forge/status';

	export let items: AttentionItem[] = [];
	export let loading = false;
	export let error: string | null = null;
	/** `${item.key}|${action.kind}` of the action in flight. */
	export let busy: string | null = null;

	const dispatch = createEventDispatcher<{ action: { item: AttentionItem; action: AttentionAction } }>();

	function actionClass(action: AttentionAction): string {
		if (action.kind === 'resume' || action.kind === 'resume-agent') {
			return 'border-[#7fb2ff]/45 text-[#a9cbff] hover:border-[#7fb2ff] hover:bg-[#7fb2ff]/10';
		}
		if (action.kind === 'dismiss') return 'border-sc-line2 text-sc-ink2 hover:border-sc-ink3 hover:text-sc-ink';
		return 'border-sc-line2 text-sc-ink2 hover:border-sc-ink hover:text-sc-ink';
	}
</script>

<article class="flex min-h-0 flex-col overflow-hidden rounded-md border border-sc-line bg-sc-panel" data-testid="agents-needs-you">
	<header class="flex items-center justify-between gap-2 border-b border-sc-line px-3.5 py-2.5">
		<h2 class="flex items-center gap-2 text-[13px] font-semibold text-sc-ink">
			Needs you
			{#if items.length > 0}
				<span class="rounded-full bg-[#e7b24a]/15 px-1.5 font-plex-mono text-[11px] font-medium text-[#e7b24a]">{items.length}</span>
			{/if}
		</h2>
		<span class="text-[11px] text-sc-ink3">Only what won't clear on its own</span>
	</header>
	<div class="min-h-0 flex-1 overflow-y-auto">
		{#if error && items.length === 0}
			<p class="m-0 px-3.5 py-4 text-[12px] text-[#f2956f]">{error}</p>
		{:else if loading && items.length === 0}
			<div class="grid gap-2 px-3.5 py-3" aria-busy="true">
				{#each [0, 1, 2] as i (i)}
					<div class="h-10 animate-pulse rounded bg-sc-raise/60"></div>
				{/each}
			</div>
		{:else if items.length === 0}
			<div class="grid place-items-center gap-1 px-3.5 py-8 text-center">
				<span class="grid h-7 w-7 place-items-center rounded-full bg-[#3cc48f]/15 text-[13px] text-[#3cc48f]" aria-hidden="true">✓</span>
				<p class="m-0 text-[12px] text-sc-ink2">Nothing needs you.</p>
				<p class="m-0 text-[11px] text-sc-ink3">No blocked or failed runs, and every job and provider is healthy.</p>
			</div>
		{:else}
			<ul class="m-0 list-none divide-y divide-sc-line p-0">
				{#each items as item (item.key)}
					<li class="grid grid-cols-[10px_minmax(0,1fr)] gap-x-2.5 px-3.5 py-2.5 hover:bg-sc-hover" data-testid="agents-attention-item">
						<span class={`mt-[5px] h-2 w-2 rounded-full ${TONE_DOT[item.tone]}`} aria-hidden="true"></span>
						<div class="min-w-0">
							<div class="flex flex-wrap items-baseline justify-between gap-x-3">
								<span class="min-w-0 text-[12.5px] font-medium text-sc-ink">{item.title}</span>
								{#if item.meta}<span class="shrink-0 text-[11px] text-sc-ink3">{item.meta}</span>{/if}
							</div>
							<p class="m-0 mt-0.5 line-clamp-2 break-words font-plex-mono text-[11px] leading-[1.45] text-sc-ink2" title={item.detail}>{item.detail}</p>
							{#if item.actions.length > 0}
								<div class="mt-1.5 flex flex-wrap gap-1.5">
									{#each item.actions as action (action.kind + action.label)}
										{#if action.kind === 'link'}
											<a href={action.href} class={`rounded border px-2 py-0.5 text-[11px] transition-colors ${actionClass(action)}`}>{action.label}</a>
										{:else}
											<button
												type="button"
												class={`rounded border px-2 py-0.5 text-[11px] transition-colors disabled:opacity-50 ${actionClass(action)}`}
												disabled={busy !== null}
												on:click={() => dispatch('action', { item, action })}
											>{busy === `${item.key}|${action.kind}` ? 'Working…' : action.label}</button>
										{/if}
									{/each}
								</div>
							{/if}
						</div>
					</li>
				{/each}
			</ul>
		{/if}
	</div>
</article>
