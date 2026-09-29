<script lang="ts">
	/** A compact grid of labelled figures. */
	export let items: Array<{ label: string; value: string; cls?: string; sub?: string; title?: string }> = [];
	export let testid = '';
</script>

<div class="grid grid-cols-[repeat(auto-fit,minmax(118px,1fr))] gap-px border border-[#1d1d1d] bg-[#1d1d1d]" data-testid={testid || undefined}>
	{#each items as item (item.label)}
		<div class="grid content-start gap-0.5 bg-[#090909] px-3 py-2" title={item.title ?? ''}>
			<div class="text-[10px] uppercase tracking-[0.14em] text-[#555]">{item.label}</div>
			<div class={`text-[15px] font-medium tabular-nums ${item.cls || 'text-white'}`}>{item.value}</div>
			{#if item.sub}<div class="text-[10.5px] text-[#666]">{item.sub}</div>{/if}
		</div>
	{/each}
</div>
