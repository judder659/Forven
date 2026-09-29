<script lang="ts">
	import { buildStrategyHref, normalizeStrategyId } from '$lib/utils/strategyLinks';

	export let strategyId = '';
	export let label: string | null = null;
	export let returnTo: string | null = null;
	export let className = '';
	export let titlePrefix = 'Open strategy';

	$: normalizedId = normalizeStrategyId(strategyId);
	$: text = (label ?? '').trim() || normalizedId || 'Unknown Strategy';
	$: href = buildStrategyHref(normalizedId, { returnTo });
	$: title = normalizedId ? `${titlePrefix}: ${normalizedId}` : text;
</script>

{#if normalizedId}
	<a
		href={href}
		title={title}
		class={`rounded-md inline-flex items-center gap-1 border border-sc-line2 bg-sc-panel2 px-2 py-0.5 font-mono text-[11px] text-sc-ink2 transition-colors hover:border-sc-line2 hover:text-sc-ink ${className}`}
	>
		{text}
	</a>
{:else}
	<span class={`rounded-md inline-flex items-center gap-1 border border-sc-line2 bg-sc-panel2 px-2 py-0.5 font-mono text-[11px] text-sc-ink3 ${className}`}>
		{text}
	</span>
{/if}
