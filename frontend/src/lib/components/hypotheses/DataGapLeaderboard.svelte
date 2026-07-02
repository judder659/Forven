<script lang="ts">
	import type { DataGapSummary } from '$lib/api';

	export let items: DataGapSummary[] = [];
	export let title = 'Top Data Gaps';
	export let subtitle = 'The missing data most often blocking crucible execution.';
</script>

<div class="terminal-card">
	<div class="border-b border-[#1a1a1a] px-4 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">{title}</h2>
		<p class="mt-1 text-xs text-[#666]">{subtitle}</p>
	</div>

	<div class="divide-y divide-[#1a1a1a]">
		{#if items.length === 0}
			<div class="px-4 py-10 text-sm text-[#666]">No ranked data gaps yet.</div>
		{:else}
			{#each items as item, index}
				<div class="px-4 py-4">
					<div class="flex items-start justify-between gap-3">
						<div class="min-w-0 flex-1">
							<div class="flex items-center gap-2">
								<span class="border border-[#333] px-2 py-1 text-[10px] font-bold uppercase tracking-widest text-[#666]">#{index + 1}</span>
								<div class="text-sm font-semibold text-white">{item.title}</div>
							</div>
							<div class="mt-2 text-[10px] uppercase tracking-wider text-[#666]">{item.missing_dataset}</div>
							{#if item.why_it_matters}
								<p class="mt-2 text-sm leading-6 text-[#888]">{item.why_it_matters}</p>
							{/if}
						</div>
						<div class="text-right text-xs">
							<div class="text-[10px] uppercase tracking-wider text-[#666]">Requests</div>
							<div class="mt-1 font-semibold text-white">{item.request_count}</div>
							<div class="mt-3 text-[10px] uppercase tracking-wider text-[#666]">Priority</div>
							<div class="mt-1 font-semibold text-white">{item.priority_score.toFixed(1)}</div>
						</div>
					</div>
					{#if item.missing_fields.length}
						<div class="mt-3 flex flex-wrap gap-2">
							{#each item.missing_fields as field}
								<span class="border border-[#222] px-2.5 py-1 text-[10px] uppercase tracking-wider text-[#666]">{field}</span>
							{/each}
						</div>
					{/if}
				</div>
			{/each}
		{/if}
	</div>
</div>
