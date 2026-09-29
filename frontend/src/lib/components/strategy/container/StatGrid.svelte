<script lang="ts">
	/** A compact grid of labelled figures. */
	export let items: Array<{ label: string; value: string; cls?: string; sub?: string; title?: string }> = [];
	export let testid = '';
</script>

<div class="grid grid-cols-[repeat(auto-fit,minmax(118px,1fr))] gap-px border border-sc-line bg-sc-line" data-testid={testid || undefined}>
	{#each items as item (item.label)}
		<div class="grid content-start gap-0.5 bg-sc-panel px-3 py-2" title={item.title ?? ''}>
			<div class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.06em] text-sc-ink3">{item.label}</div>
			<div class={`text-[15px] font-medium font-plex-mono tabular-nums ${item.cls || 'text-sc-ink'}`}>{item.value}</div>
			{#if item.sub}<div class="text-[10.5px] text-sc-ink3">{item.sub}</div>{/if}
		</div>
	{/each}
</div>
