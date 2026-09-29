<script lang="ts">
	/** Things the operator can act on, most severe first. */
	import type { AttentionItem } from '$lib/utils/liveDashboard';

	export let items: AttentionItem[] = [];

	const ICON: Record<AttentionItem['severity'], string> = { critical: '■', warning: '▲', info: '●' };
	const TONE: Record<AttentionItem['severity'], string> = {
		critical: 'text-red-400',
		warning: 'text-amber-300',
		info: 'text-sc-ink2',
	};
</script>

<div class="rounded-md flex min-h-0 flex-col border border-sc-line bg-sc-panel" data-testid="attention-panel">
	<div class="border-b border-sc-line px-3 py-2">
		<h2 class="font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink2">
			Needs attention {#if items.length > 0}<span class="text-sc-ink3">({items.length})</span>{/if}
		</h2>
	</div>
	{#if items.length === 0}
		<div class="px-3 py-4 text-xs text-emerald-400">✓ Nothing needs you right now.</div>
	{:else}
		<ul class="max-h-[260px] divide-y divide-sc-line overflow-y-auto">
			{#each items as item (item.id)}
				<li>
					<svelte:element
						this={item.href ? 'a' : 'div'}
						href={item.href}
						class="flex gap-2 px-3 py-1.5 text-xs {item.href ? 'hover:bg-sc-panel2' : ''}"
					>
						<span class="mt-px shrink-0 {TONE[item.severity]}" aria-label={item.severity}>{ICON[item.severity]}</span>
						<span class="min-w-0">
							<span class="text-sc-ink">{item.title}</span>
							{#if item.detail}
								<span class="block truncate text-[11px] text-sc-ink3" title={item.detail}>{item.detail}</span>
							{/if}
						</span>
					</svelte:element>
				</li>
			{/each}
		</ul>
	{/if}
</div>
