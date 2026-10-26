<script lang="ts">
	import type { ReleaseNotesSection } from '$lib/api/updates';

	export let sections: ReleaseNotesSection[] = [];
	/** Collapse behind "Show all" past this many items; 0 shows everything. */
	export let limit = 0;

	let expanded = false;

	const TONE: Record<string, string> = {
		new: 'bg-emerald-400',
		fixes: 'bg-sky-400',
		safety: 'bg-amber-400',
	};

	function tone(title: string): string {
		return TONE[title.trim().toLowerCase()] ?? 'bg-sc-ink3';
	}

	$: total = sections.reduce((n, s) => n + s.items.length, 0);
	$: collapsed = limit > 0 && total > limit && !expanded;
	// Trim to `limit` items across sections, keeping section order.
	$: shown = (() => {
		if (!collapsed) return sections;
		let left = limit;
		const out: ReleaseNotesSection[] = [];
		for (const s of sections) {
			if (left <= 0) break;
			const items = s.items.slice(0, left);
			left -= items.length;
			out.push({ title: s.title, items });
		}
		return out;
	})();
</script>

<div class="space-y-3">
	{#each shown as section, i (i)}
		<div>
			{#if section.title}
				<h4 class="mb-1.5 flex items-center gap-2 font-plex-cond text-[11px] font-medium uppercase tracking-[0.08em] text-sc-ink3">
					<span class="h-1.5 w-1.5 rounded-full {tone(section.title)}" aria-hidden="true"></span>
					{section.title}
				</h4>
			{/if}
			<ul class="space-y-1">
				{#each section.items as item, j (j)}
					<li class="flex gap-2 text-[13px] leading-snug text-sc-ink2">
						<span class="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-sc-ink4" aria-hidden="true"></span>
						<span>{item}</span>
					</li>
				{/each}
			</ul>
		</div>
	{/each}
	{#if limit > 0 && total > limit}
		<button
			type="button"
			class="text-[12px] font-medium text-sc-ink3 hover:text-sc-ink transition-colors"
			on:click={() => (expanded = !expanded)}
		>
			{expanded ? 'Show less' : `Show all ${total} changes`}
		</button>
	{/if}
</div>
