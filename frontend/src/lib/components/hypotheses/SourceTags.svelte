<script lang="ts">
	export let tags: string[] = [];
	export let size: 'sm' | 'md' = 'sm';

	type Palette = {
		label: string;
		classes: string;
	};

	// Neutral terminal chips — source type is conveyed by the label, not color.
	const NEUTRAL = 'border-[#333] text-[#888]';
	const PALETTE: Record<string, Palette> = {
		youtube: { label: 'YouTube', classes: NEUTRAL },
		reddit: { label: 'Reddit', classes: NEUTRAL },
		github: { label: 'GitHub', classes: NEUTRAL },
		blog: { label: 'Blog', classes: NEUTRAL },
		forum: { label: 'Forum', classes: NEUTRAL },
	};

	function palette(tag: string): Palette {
		const key = tag.toLowerCase();
		return PALETTE[key] ?? { label: key, classes: NEUTRAL };
	}

	$: sizeClass = size === 'md' ? 'px-2.5 py-1 text-[10px]' : 'px-2 py-0.5 text-[10px]';
</script>

{#if tags.length > 0}
	<div class="flex flex-wrap items-center gap-1.5">
		{#each tags as tag}
			{@const p = palette(tag)}
			<span class={`inline-flex border ${sizeClass} font-semibold uppercase tracking-[0.18em] ${p.classes}`}>
				{p.label}
			</span>
		{/each}
	</div>
{/if}
