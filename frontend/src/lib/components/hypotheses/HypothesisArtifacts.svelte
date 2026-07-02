<script lang="ts">
	import type { HypothesisArtifact } from '$lib/api';
	import { safeHref } from '$lib/utils/url';

	export let artifacts: HypothesisArtifact[] = [];
	export let includeContent = false;
	export let onToggleContent: () => void = () => {};

	let expandedIds: Set<string> = new Set();

	function toggleArtifact(id: string): void {
		const next = new Set(expandedIds);
		if (next.has(id)) next.delete(id); else next.add(id);
		expandedIds = next;
	}

	function formatBytes(n: number | null | undefined): string {
		if (n === null || n === undefined) return '—';
		if (n < 1024) return `${n} B`;
		if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
		return `${(n / 1024 / 1024).toFixed(1)} MB`;
	}
</script>

<div class="terminal-card">
	<div class="border-b border-[#1a1a1a] px-4 py-2">
		<div class="flex flex-wrap items-center justify-between gap-3">
			<div>
				<h2 class="text-[10px] font-bold uppercase tracking-widest text-[#888]">Source Artifacts</h2>
				<p class="mt-1 text-xs text-[#666]">Keep the original source visible so the operator can verify the thesis.</p>
			</div>
			{#if artifacts.some((a) => a.cached_content_hash)}
				<button
					type="button"
					class="terminal-button text-[10px]"
					on:click={onToggleContent}
				>
					{includeContent ? 'Hide cached content' : 'Load cached content'}
				</button>
			{/if}
		</div>
	</div>

	<div class="divide-y divide-[#1a1a1a]">
		{#if artifacts.length === 0}
			<div class="px-4 py-10 text-sm text-[#666]">No source artifacts attached yet.</div>
		{:else}
			{#each artifacts as artifact}
				<div class="px-4 py-4">
					<div class="flex flex-wrap items-center gap-2">
						<span class="border border-[#333] px-2.5 py-1 text-[10px] uppercase tracking-wider text-[#888]">{artifact.source_type}</span>
						<a href={safeHref(artifact.source_ref)} target="_blank" rel="noreferrer noopener" class="text-sm font-semibold text-white transition hover:text-[#888]">
							{artifact.source_title}
						</a>
						{#if artifact.cached_content_hash}
							<span class="text-[10px] uppercase tracking-wider text-[#666]">
								cached {formatBytes(artifact.content_bytes)}
							</span>
						{/if}
					</div>
					<p class="mt-3 text-sm leading-6 text-[#888]">{artifact.claimed_edge}</p>
					<p class="mt-2 text-sm leading-6 text-[#666]">{artifact.implementation_summary}</p>
					{#if artifact.adaptation_notes}
						<p class="mt-3 text-[10px] uppercase tracking-wider text-[#666]">Adaptation: <span class="normal-case tracking-normal text-[#888]">{artifact.adaptation_notes}</span></p>
					{/if}
					{#if artifact.caveats}
						<p class="mt-2 text-[10px] uppercase tracking-wider text-[#666]">Caveats: <span class="normal-case tracking-normal text-[#888]">{artifact.caveats}</span></p>
					{/if}
					{#if artifact.cached_content}
						<button
							type="button"
							class="mt-3 text-[11px] uppercase tracking-wider text-[#888] hover:text-white"
							on:click={() => toggleArtifact(artifact.id)}
						>
							{expandedIds.has(artifact.id) ? '▾ Hide content' : '▸ View cached content'}
						</button>
						{#if expandedIds.has(artifact.id)}
							<pre class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap border border-[#222] bg-black p-3 text-[11px] leading-5 text-[#888]">{artifact.cached_content}</pre>
						{/if}
					{/if}
				</div>
			{/each}
		{/if}
	</div>
</div>
