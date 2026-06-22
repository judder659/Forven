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

<div class="rounded-3xl border border-[#1f1f1f] bg-[#090909]">
	<div class="border-b border-[#1f1f1f] px-5 py-4">
		<div class="flex flex-wrap items-center justify-between gap-3">
			<div>
				<h2 class="text-sm font-semibold uppercase tracking-[0.22em] text-gray-300">Source Artifacts</h2>
				<p class="mt-1 text-xs text-gray-500">Keep the original source visible so the operator can verify the thesis.</p>
			</div>
			{#if artifacts.some((a) => a.cached_content_hash)}
				<button
					type="button"
					class="border border-[#2d2d2d] bg-[#111] px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.18em] text-gray-300 transition hover:border-cyan-300 hover:text-white"
					on:click={onToggleContent}
				>
					{includeContent ? 'Hide cached content' : 'Load cached content'}
				</button>
			{/if}
		</div>
	</div>

	<div class="divide-y divide-[#171717]">
		{#if artifacts.length === 0}
			<div class="px-5 py-10 text-sm text-gray-500">No source artifacts attached yet.</div>
		{:else}
			{#each artifacts as artifact}
				<div class="px-5 py-4">
					<div class="flex flex-wrap items-center gap-2">
						<span class="rounded-full border border-[#262626] bg-black/60 px-2.5 py-1 text-[10px] uppercase tracking-[0.18em] text-gray-400">{artifact.source_type}</span>
						<a href={safeHref(artifact.source_ref)} target="_blank" rel="noreferrer noopener" class="text-sm font-semibold text-cyan-300 transition hover:text-cyan-200">
							{artifact.source_title}
						</a>
						{#if artifact.cached_content_hash}
							<span class="text-[10px] uppercase tracking-[0.18em] text-gray-500">
								cached {formatBytes(artifact.content_bytes)}
							</span>
						{/if}
					</div>
					<p class="mt-3 text-sm leading-6 text-gray-300">{artifact.claimed_edge}</p>
					<p class="mt-2 text-sm leading-6 text-gray-500">{artifact.implementation_summary}</p>
					{#if artifact.adaptation_notes}
						<p class="mt-3 text-xs uppercase tracking-[0.18em] text-gray-500">Adaptation: <span class="normal-case tracking-normal text-gray-400">{artifact.adaptation_notes}</span></p>
					{/if}
					{#if artifact.caveats}
						<p class="mt-2 text-xs uppercase tracking-[0.18em] text-gray-500">Caveats: <span class="normal-case tracking-normal text-gray-400">{artifact.caveats}</span></p>
					{/if}
					{#if artifact.cached_content}
						<button
							type="button"
							class="mt-3 text-[11px] uppercase tracking-[0.18em] text-cyan-400 hover:text-cyan-200"
							on:click={() => toggleArtifact(artifact.id)}
						>
							{expandedIds.has(artifact.id) ? '▾ Hide content' : '▸ View cached content'}
						</button>
						{#if expandedIds.has(artifact.id)}
							<pre class="mt-2 max-h-96 overflow-auto whitespace-pre-wrap border border-[#1f1f1f] bg-black/60 p-3 text-[11px] leading-5 text-gray-300">{artifact.cached_content}</pre>
						{/if}
					{/if}
				</div>
			{/each}
		{/if}
	</div>
</div>
