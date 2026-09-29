<script lang="ts">
	/** What needs the operator, most severe first. Each item jumps to the strategy and tab. */
	import { createEventDispatcher } from 'svelte';
	import type { AttentionItem, SideTab } from '$lib/utils/tradingDesk/attention';

	export let items: AttentionItem[] = [];

	const dispatch = createEventDispatcher<{ show: { strategyId: string; tab?: SideTab } }>();

	$: counts = items.reduce(
		(acc, item) => ({ ...acc, [item.severity]: acc[item.severity] + 1 }),
		{ fail: 0, caution: 0, info: 0 } as Record<AttentionItem['severity'], number>
	);

	const BAR: Record<AttentionItem['severity'], string> = { fail: 'bg-[#e5574f]', caution: 'bg-[#e7b24a]', info: 'bg-sc-line2' };
	const TAG: Record<AttentionItem['severity'], string> = { fail: 'text-[#e5574f]', caution: 'text-[#e7b24a]', info: 'text-sc-ink3' };
	const WORD: Record<AttentionItem['severity'], string> = { fail: 'Critical', caution: 'Warning', info: 'Note' };
</script>

<section class="rounded-md border border-sc-line bg-sc-panel" aria-label="Needs attention" data-testid="desk-attention">
	{#if items.length === 0}
		<div class="flex flex-wrap items-center gap-3 px-3 py-2">
			<h2 class="text-[13px] font-semibold text-sc-ink">Nothing needs you</h2>
			<span class="text-[12px] text-sc-ink3">Every strategy was checked recently and nothing was refused this week.</span>
		</div>
	{:else}
		<div class="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-sc-line px-3 py-2">
			<h2 class="text-[13px] font-semibold text-sc-ink">Needs attention</h2>
			<span class="text-[12px] text-sc-ink3">
				{#if counts.fail}{`${counts.fail} critical · `}{/if}{counts.caution} warning{counts.caution === 1 ? '' : 's'} · {counts.info} note{counts.info === 1 ? '' : 's'}
			</span>
		</div>
		<ul class="grid lg:grid-cols-2">
			{#each items as item (item.id)}
				<li class="grid min-w-0 grid-cols-[4px_minmax(0,1fr)_auto] items-start gap-2.5 border-b border-sc-line px-3 py-2 lg:[&:nth-child(odd)]:border-r">
					<span class={`self-stretch rounded-sm ${BAR[item.severity]}`}></span>
					<div class="min-w-0">
						<div class="text-[13px] font-semibold text-sc-ink">
							<span class={`mr-1.5 font-plex-cond text-[10.5px] font-semibold uppercase tracking-[0.08em] ${TAG[item.severity]}`}>{WORD[item.severity]}</span>{item.title}
						</div>
						<div class="text-[12px] text-sc-ink2">{item.body}</div>
					</div>
					{#if item.strategyId}
						<button type="button" class="whitespace-nowrap text-[12px] text-sc-ink3 underline underline-offset-2 hover:text-sc-ink" on:click={() => dispatch('show', { strategyId: item.strategyId ?? '', tab: item.tab })}>Show</button>
					{:else}
						<span></span>
					{/if}
				</li>
			{/each}
		</ul>
	{/if}
</section>
