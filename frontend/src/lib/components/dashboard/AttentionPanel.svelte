<script lang="ts">
	/** Things the operator can act on, most severe first. */
	import type { AttentionItem } from '$lib/utils/liveDashboard';

	export let items: AttentionItem[] = [];

	const ICON: Record<AttentionItem['severity'], string> = { critical: '■', warning: '▲', info: '●' };
	const TONE: Record<AttentionItem['severity'], string> = {
		critical: 'text-red-400',
		warning: 'text-amber-300',
		info: 'text-gray-400',
	};
</script>

<div class="flex min-h-0 flex-col border border-[#222] bg-[#050505]" data-testid="attention-panel">
	<div class="border-b border-[#222] px-3 py-2">
		<h2 class="text-[10px] font-bold uppercase tracking-wider text-gray-400">
			Needs attention {#if items.length > 0}<span class="text-gray-600">({items.length})</span>{/if}
		</h2>
	</div>
	{#if items.length === 0}
		<div class="px-3 py-4 text-xs text-emerald-400">✓ Nothing needs you right now.</div>
	{:else}
		<ul class="max-h-[260px] divide-y divide-[#1a1a1a] overflow-y-auto">
			{#each items as item (item.id)}
				<li>
					<svelte:element
						this={item.href ? 'a' : 'div'}
						href={item.href}
						class="flex gap-2 px-3 py-1.5 text-xs {item.href ? 'hover:bg-[#0d0d0d]' : ''}"
					>
						<span class="mt-px shrink-0 {TONE[item.severity]}" aria-label={item.severity}>{ICON[item.severity]}</span>
						<span class="min-w-0">
							<span class="text-gray-200">{item.title}</span>
							{#if item.detail}
								<span class="block truncate text-[11px] text-gray-500" title={item.detail}>{item.detail}</span>
							{/if}
						</span>
					</svelte:element>
				</li>
			{/each}
		</ul>
	{/if}
</div>
