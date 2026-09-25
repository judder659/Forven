<script lang="ts">
	import { getIdea, type Idea } from '$lib/api/ideas';
	import { safeHref } from '$lib/utils/url';

	/** The strategy's hypothesis_id (the idea it tests). */
	export let ideaId: string;
	/** Short label, e.g. the idea's display id. */
	export let label: string = ideaId;
	/** The strategy this panel sits on; left out of the sibling list. */
	export let strategyId: string | null = null;

	let open = false;
	let loading = false;
	let idea: Idea | null = null;
	let errorMsg: string | null = null;

	$: siblings = (idea?.strategies ?? []).filter((row) => row.id !== strategyId);

	async function toggle(): Promise<void> {
		open = !open;
		if (!open || idea || loading) return;
		loading = true;
		errorMsg = null;
		try {
			idea = await getIdea(ideaId);
		} catch (err) {
			errorMsg = err instanceof Error ? err.message : 'Could not load the idea.';
		} finally {
			loading = false;
		}
	}
</script>

<span class="relative inline-block">
	<button
		type="button"
		class="text-[11px] uppercase tracking-widest text-[#888] transition-colors hover:text-white"
		aria-expanded={open}
		on:click={toggle}
	>
		Idea {label} {open ? '▴' : '▾'}
	</button>
	{#if open}
		<div
			class="absolute left-0 top-full z-40 mt-2 w-[min(32rem,90vw)] space-y-2 border border-[#222] bg-[#050505] p-4 text-left text-xs normal-case tracking-normal text-[#ccc] shadow-xl"
		>
			{#if loading}
				<p class="text-[#666]">Loading…</p>
			{:else if errorMsg}
				<p class="text-red-400">{errorMsg}</p>
			{:else if idea}
				<div class="text-sm font-bold text-white">{idea.title}</div>
				{#if idea.market_thesis}
					<div><span class="text-[10px] uppercase tracking-wider text-[#666]">Thesis</span><p>{idea.market_thesis}</p></div>
				{/if}
				{#if idea.mechanism}
					<div><span class="text-[10px] uppercase tracking-wider text-[#666]">Mechanism</span><p>{idea.mechanism}</p></div>
				{/if}
				{#if idea.disproof}
					<div>
						<span class="text-[10px] uppercase tracking-wider text-[#666]">What would disprove it</span>
						<p>{idea.disproof}</p>
					</div>
				{/if}
				{#if (idea.target_assets ?? []).length || (idea.target_timeframes ?? []).length}
					<p class="text-[#888]">
						{(idea.target_assets ?? []).join(', ')}{(idea.target_timeframes ?? []).length ? ` · ${(idea.target_timeframes ?? []).join(', ')}` : ''}
					</p>
				{/if}
				{#each idea.artifacts as artifact (artifact.id)}
					{#if artifact.source_ref && safeHref(artifact.source_ref) !== '#'}
						<a
							href={safeHref(artifact.source_ref)}
							target="_blank"
							rel="noopener noreferrer"
							class="block truncate text-[#888] underline hover:text-white"
						>
							Source: {artifact.source_title || artifact.source_ref}
						</a>
					{/if}
				{/each}
				{#if siblings.length}
					<div>
						<span class="text-[10px] uppercase tracking-wider text-[#666]">Other strategies from this idea</span>
						<ul class="mt-1 space-y-0.5">
							{#each siblings as row (row.id)}
								<li>
									<a href={`/lab/strategy/${encodeURIComponent(row.id)}`} class="font-mono text-[#aaa] hover:text-white">
										{row.display_id || row.id}
									</a>
									<span class="text-[#666]">· {row.symbol ?? ''} {row.timeframe ?? ''} · {row.stage ?? ''}</span>
								</li>
							{/each}
						</ul>
					</div>
				{/if}
			{/if}
		</div>
	{/if}
</span>
