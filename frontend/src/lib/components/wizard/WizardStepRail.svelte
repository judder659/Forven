<script lang="ts">
	export let steps: Array<{
		id: string;
		label: string;
		critical: boolean;
		satisfied: boolean;
	}>;
	export let activeIndex: number;
	export let onSelect: (index: number) => void;
	export let onSkipAll: () => void;

	function statusSuffix(step: { critical: boolean; satisfied: boolean }): string {
		if (step.critical && !step.satisfied) return ' (needs attention)';
		if (step.satisfied) return ' (complete)';
		return ' (pending)';
	}
</script>

<nav class="flex flex-col h-full border-r border-[#222] bg-[#050505] w-[200px] p-3">
	<ol class="flex-1 space-y-px">
		{#each steps as step, i (step.id)}
			<li>
				<button
					type="button"
					aria-current={i === activeIndex ? 'step' : undefined}
					aria-label={step.label + statusSuffix(step)}
					class="w-full flex items-center gap-2 text-left px-2 py-2 text-xs uppercase tracking-wider border-l-2 transition-colors focus:outline-none
					       {i === activeIndex ? 'border-white bg-[#111] text-white' : 'border-transparent text-[#888] hover:bg-[#111] hover:text-white'}"
					on:click={() => onSelect(i)}
				>
					<span class="w-4 flex-shrink-0 text-xs" aria-hidden="true">
						{#if step.critical && !step.satisfied}
							<span class="text-yellow-400">△</span>
						{:else if step.satisfied}
							<span class="text-emerald-400">✓</span>
						{:else}
							<span class="text-[#555]">○</span>
						{/if}
					</span>
					<span class="flex-1 truncate">{step.label}</span>
				</button>
			</li>
		{/each}
	</ol>
	<button
		type="button"
		class="mt-4 text-[10px] uppercase tracking-wider text-[#666] hover:text-white text-left px-2 py-1 focus:outline-none"
		on:click={onSkipAll}
	>
		Skip all — I'll configure later
	</button>
</nav>
