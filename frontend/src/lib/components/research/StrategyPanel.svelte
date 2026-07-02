<script lang="ts">
	import { createEventDispatcher } from 'svelte';
	import type { Strategy } from '$lib/api';

	export let strategies: Strategy[] = [];
	export let selectedStrategy: Strategy | null = null;

	const dispatch = createEventDispatcher<{
		select: { strategy: Strategy };
		create: void;
	}>();

	let searchQuery = '';

	$: filteredStrategies = strategies.filter(s => 
		s.name.toLowerCase().includes(searchQuery.toLowerCase()) || 
		(s.description && s.description.toLowerCase().includes(searchQuery.toLowerCase()))
	);

	function getMarketTypeLabel(description: string | undefined): string | null {
		if (!description) return null;
		const lower = description.toLowerCase();
		if (lower.includes('trend')) return 'TREND';
		if (lower.includes('mean reversion') || lower.includes('rang')) return 'RANGE';
		if (lower.includes('volatil') || lower.includes('breakout')) return 'BREAK';
		return null;
	}
</script>

<div class="flex flex-col h-full bg-[#050505]">
	<!-- Header / Search -->
	<div class="p-2 border-b border-[#222] flex gap-2">
		<input 
			type="text" 
			bind:value={searchQuery}
			placeholder="Search strategies..." 
			class="terminal-input flex-1"
		/>
		<button 
			class="terminal-button-icon w-8 h-8 flex items-center justify-center bg-[#222] hover:bg-[#333] border border-[#333]"
			title="Create New Strategy"
			on:click={() => dispatch('create')}
		>
			<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 4v16m8-8H4" /></svg>
		</button>
	</div>

	<!-- List -->
	<div class="flex-1 overflow-y-auto">
		{#if filteredStrategies.length === 0}
			<div class="p-4 text-xs text-[#666] text-center">No strategies found</div>
		{:else}
			{#each filteredStrategies as strategy}
				{@const isSelected = selectedStrategy?.name === strategy.name}
				{@const type = getMarketTypeLabel(strategy.description)}
				<div 
					class="terminal-list-item group {isSelected ? 'active' : ''}"
					on:click={() => dispatch('select', { strategy })}
                    on:keydown={(e) => e.key === 'Enter' && dispatch('select', { strategy })}
                    role="button"
                    tabindex="0"
				>
					<div class="flex flex-col min-w-0">
						<div class="flex items-center gap-2">
							<span class="font-bold truncate">{strategy.name}</span>
							<span class="text-[9px] text-[#666] border border-[#333] px-1">v{strategy.version}</span>
						</div>
						{#if type}
							<span class="text-[9px] text-[#666] mt-1 uppercase tracking-wider">{type}</span>
						{/if}
					</div>
					<div class="text-[10px] text-[#555]">
						{Object.keys(strategy.parameters).length} params
					</div>
				</div>
			{/each}
		{/if}
	</div>
</div>
